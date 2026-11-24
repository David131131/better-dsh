import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { apply } from "./checkpoint.mjs";

const high = { provider: "deepseek-official", model: "deepseek-flash", reasoningEffort: "high" };
const head = "a".repeat(40);

function setup(t, overrides = {}) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "better-dsh-test-")));
  writeFileSync(join(root, "src.py"), "");
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const hooks = new Map();
  const commands = [];
  const state = { status: "", patch: "first patch", failingCommand: undefined, changeDuringCheck: false,
    jobs: [], head, root, workdir: root, shellOverrides: new Map() };
  const policy = { mode: "workspace-write", workspaceRoot: root };
  const ctx = {
    on: (event, hook) => hooks.set(event, hook),
    get: () => ({ list: () => {
      if (state.jobsError) throw new Error("jobs unavailable");
      return state.jobs;
    } }),
    workingDirectory: { get: () => state.workdir },
    sandboxPolicy: { resolve: () => policy },
    shell: {
      resolve: (spec) => spec,
      async execute(spec) {
        const { command } = spec;
        commands.push(command);
        assert.equal(spec.sandboxPolicy, policy);
        let text = "";
        let exitCode = 0;
        if (command.includes(" rev-parse ")) text = `${state.root}\n${state.head}\n${state.status}`;
        else if (command.includes(" diff ")) text = state.patch;
        else if (command === state.failingCommand) exitCode = 1;
        if (command === "regression" && state.changeDuringCheck) state.patch += " mutated";
        const override = state.shellOverrides.get(command);
        return { result: async () => ({ exitCode, stdout: { text }, signal: null, ...override }) };
      },
    },
  };
  const agent = { session: { id: "session-example", deriveMessages: () => [{ role: state.lastRole ?? "tool" }] },
    inbox: { nextTurn: [], nextStep: [] } };
  const controller = new AbortController();
  const signal = controller.signal;
  const config = { enabled: true, mode: "stop", workspace: root, paths: ["src.py"],
    checks: [{ name: "public", command: "public" }, { name: "regression", command: "regression" }],
    ...overrides };
  apply(ctx, config);
  const pre = (step, extra = {}) => hooks.get("agent/pre-step")({
    agent, turn: 1, step, messages: [], modelSelection: high, signal, ...extra,
  }, async () => ({ kind: "enter", messages: [] }));
  async function stream(extra = {}) {
    const response = hooks.get("llm/stream")({
      ...high, sessionId: agent.session.id, signal, messages: [{ role: "tool" }], ...extra,
    }, async function* () { yield { type: "upstream" }; });
    return Array.fromAsync(response);
  }
  return { agent, commands, controller, hooks, policy, pre, root, state, stream };
}

test("disabled config registers no hooks", (t) => {
  assert.equal(setup(t, { enabled: false }).hooks.size, 0);
});

test("low is opt-in and must match both the turn and stream route", async (t) => {
  const low = { ...high, reasoningEffort: "low" };
  for (const options of [{}, { effort: "low" }]) {
    const { commands, pre, state, stream } = setup(t, options);
    await pre(1, { modelSelection: low });
    state.status = " M src.py\0";
    await pre(2, { modelSelection: low });
    assert.deepEqual(await stream(), [{ type: "upstream" }]);
    const response = await stream(low);
    if (options.effort === "low") {
      assert.equal(response.at(-1).reason.kind, "stop");
    } else {
      assert.deepEqual(response, [{ type: "upstream" }]);
    }
    assert.equal(commands.includes("public"), options.effort === "low");
  }
});

test("two successful checks yield a resumable local answer and no upstream stream", async (t) => {
  const { commands, pre, state, stream } = setup(t);
  await pre(1);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
  state.status = " M src.py\0";
  await pre(2);
  const response = await stream();
  assert.deepEqual(response.map((chunk) => chunk.type), ["block-start", "text-delta", "block-end", "finish"]);
  assert.match(response[1].text, /src\.py[\s\S]*public[\s\S]*regression[\s\S]*--session-id session-example/);
  assert.equal(response.at(-1).reason.kind, "stop");
  assert.deepEqual(commands.filter((command) => command === "public" || command === "regression"),
    ["public", "regression"]);
});

test("a failing or changing patch delegates to the upstream model", async (t) => {
  for (const changed of [false, true]) {
    const { pre, state, stream } = setup(t);
    await pre(1);
    state.status = " M src.py\0";
    state.changeDuringCheck = changed;
    if (!changed) state.failingCommand = "regression";
    await pre(2);
    assert.deepEqual(await stream(), [{ type: "upstream" }]);
  }
});

test("an exit failure produces one sourced notice per turn with a configured hint", async (t) => {
  const checks = ["public", "regression"].map((command) => ({
    name: command, command, feedbackHint: "Look at the public contract.",
  }));
  const { pre, state, stream } = setup(t, { checks, failureFeedback: true });
  await pre(1);
  state.status = " M src.py\0";
  state.failingCommand = "regression";
  const decision = await pre(2);
  const [message] = decision.messages;
  assert.equal(message.source.kind, "better-dsh-checkpoint");
  assert.match(message.content[0].text, /regression[\s\S]*exit-code[\s\S]*public contract/);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
  state.patch = "still failing";
  assert.deepEqual((await pre(3)).messages, []);
  state.patch = "fixed";
  state.failingCommand = undefined;
  await pre(4);
  assert.equal((await stream()).at(-1).reason.kind, "stop");
});

test("observation, timeouts, or no pending tool continuation do not send feedback", async (t) => {
  for (const reason of ["observe", "timeout", "no-tool"]) {
    const { pre, state, stream } = setup(t, { failureFeedback: true, ...(reason === "observe" && { mode: "observe" }) });
    await pre(1);
    state.status = " M src.py\0";
    state.failingCommand = "regression";
    if (reason === "timeout") state.shellOverrides.set("regression", { timedOut: true });
    if (reason === "no-tool") state.lastRole = "assistant";
    assert.deepEqual((await pre(2)).messages, [], reason);
    assert.deepEqual(await stream(), [{ type: "upstream" }], reason);
  }
});

test("an edit between pre-step and stream invalidates a passing checkpoint", async (t) => {
  const { pre, state, stream } = setup(t);
  await pre(1);
  state.status = " M src.py\0";
  await pre(2);
  state.patch = "different patch after checks";
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
});

test("a third check must pass before the plugin stops", async (t) => {
  const checks = ["public", "regression", "boundary"].map((command) => ({ name: command, command }));
  const { commands, pre, state, stream } = setup(t, { checks });
  await pre(1);
  state.status = " M src.py\0";
  state.failingCommand = "boundary";
  await pre(2);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);

  await pre(3);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
  assert.deepEqual(commands.filter((command) => checks.some((check) => check.command === command)),
    ["public", "regression", "boundary"]);

  state.patch = "corrected patch";
  state.failingCommand = undefined;
  await pre(4);
  const response = await stream();
  assert.equal(response.at(-1).reason.kind, "stop");
  assert.match(response[1].text, /3 项配置检查通过[\s\S]*public[\s\S]*regression[\s\S]*boundary/);
});

test("enabled without a mode observes and preserves the upstream stream", async (t) => {
  const { commands, pre, state, stream } = setup(t, { mode: undefined });
  await pre(1);
  state.status = " M src.py\0";
  await pre(2);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
  state.patch = "later patch";
  await pre(3);
  assert.deepEqual(await stream(), [{ type: "upstream" }]);
  assert.deepEqual(commands.filter((command) => command === "public" || command === "regression"),
    ["public", "regression"]);
});

test("unsupported changes never run configured checks", async (t) => {
  for (const status of ["?? new.py\0", " D src.py\0", "R  src.py\0old.py\0", "T  src.py\0", "A  src.py\0"]) {
    const { pre, state, stream, commands } = setup(t);
    await pre(1);
    state.status = status;
    await pre(2);
    assert.deepEqual(await stream(), [{ type: "upstream" }], status);
    assert.ok(!commands.includes("public"), status);
  }
});

test("changed HEAD, large diff, and symlink never stop", async (t) => {
  for (const condition of ["head", "large", "symlink"]) {
    const { pre, state, stream, commands, root } = setup(t);
    await pre(1);
    state.status = " M src.py\0";
    if (condition === "head") state.head = "b".repeat(40);
    if (condition === "large") state.shellOverrides.set(
      "git --no-optional-locks -c core.fsmonitor=false -c core.quotepath=false diff --no-ext-diff --no-textconv --binary HEAD --",
      { stdout: { text: "cut off", truncated: true } });
    if (condition === "symlink") {
      rmSync(join(root, "src.py"));
      symlinkSync("target.py", join(root, "src.py"));
    }
    await pre(2);
    assert.deepEqual(await stream(), [{ type: "upstream" }], condition);
    assert.ok(!commands.includes("public"), condition);
  }
});

test("sandbox, Git, jobs, and working directory failures leave upstream available", async (t) => {
  for (const condition of ["root", "git", "jobs", "subdirectory", "git-root"]) {
    const { pre, state, stream, commands, root, policy } = setup(t);
    if (condition === "root") policy.workspaceRoot = tmpdir();
    if (condition === "git") state.shellOverrides.set(
      "git --no-optional-locks -c core.fsmonitor=false -c core.quotepath=false rev-parse --show-toplevel HEAD && git --no-optional-locks -c core.fsmonitor=false -c core.quotepath=false status --porcelain=v1 -z --untracked-files=all --no-renames",
      { exitCode: 127 });
    if (condition === "jobs") state.jobsError = true;
    if (condition === "subdirectory") state.workdir = join(root, "src.py");
    if (condition === "git-root") state.root = realpathSync(tmpdir());
    await pre(1);
    state.status = " M src.py\0";
    await pre(2);
    assert.deepEqual(await stream(), [{ type: "upstream" }], condition);
    assert.ok(!commands.includes("public"), condition);
  }
});

test("timeout, rejected sandbox, and cancellation cannot turn a pass into a stop", async (t) => {
  for (const result of [{ timedOut: true }, { sandbox: { denied: true } }, { aborted: true }, null]) {
    const { pre, state, stream, commands, controller } = setup(t);
    await pre(1);
    state.status = " M src.py\0";
    if (result) state.shellOverrides.set("public", result);
    else controller.abort();
    await pre(2);
    assert.deepEqual(await stream(), [{ type: "upstream" }]);
    assert.ok(!commands.includes("regression"));
  }
});

test("dirty starts, disallowed files, queued user input, and active jobs do not trigger checks", async (t) => {
  for (const situation of ["dirty", "files", "input", "job"]) {
    const { agent, commands, pre, state, stream } = setup(t);
    if (situation === "dirty") state.status = " M src.py\0";
    await pre(1);
    state.status = situation === "files" ? " M other.py\0" : " M src.py\0";
    if (situation === "job") state.jobs.push({ owner: agent.session.id, status: "running" });
    await pre(2, situation === "input" ? { messages: [{ role: "user" }] } : {});
    assert.deepEqual(await stream(), [{ type: "upstream" }]);
    assert.ok(!commands.includes("public"));
  }
});
