import { createHash, randomUUID } from "node:crypto";
import { lstatSync, realpathSync, writeSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { performance } from "node:perf_hooks";

export const name = "better-dsh-checkpoint";
export const inject = ["llm", "shell", "sandboxPolicy", "workingDirectory"];

const git = "git --no-optional-locks -c core.fsmonitor=false -c core.quotepath=false";
const skipped = (reason) => ({ reason });

function configure(raw) {
  if (raw?.enabled !== true) return undefined;
  const mode = raw.mode ?? "observe";
  if (mode !== "stop" && mode !== "observe") {
    throw new Error("better-dsh: mode must be stop or observe");
  }
  const effort = raw.effort ?? "high";
  if (effort !== "high" && effort !== "low") {
    throw new Error("better-dsh: effort must be high or low");
  }
  if (raw.failureFeedback !== undefined && typeof raw.failureFeedback !== "boolean") {
    throw new Error("better-dsh: failureFeedback must be a boolean");
  }
  if (typeof raw.workspace !== "string" || !isAbsolute(raw.workspace)) {
    throw new Error("better-dsh: workspace must be an absolute repository path");
  }
  if (!Array.isArray(raw.checks) || ![2, 3].includes(raw.checks.length) ||
      raw.checks.some((check) => typeof check?.name !== "string" || !check.name.trim() ||
        typeof check.command !== "string" || !check.command.trim() ||
        (check.feedbackHint !== undefined &&
          (typeof check.feedbackHint !== "string" || check.feedbackHint.length > 240))) ||
      new Set(raw.checks.map(({ command }) => command.trim())).size !== raw.checks.length) {
    throw new Error("better-dsh: configure two or three distinct checks with a name and command");
  }
  if (!Array.isArray(raw.paths) || !raw.paths.length ||
      raw.paths.some((file) => typeof file !== "string" || !file || isAbsolute(file) ||
        file.includes("\0") || file.includes("\\") ||
        file.split("/").some((part) => !part || part === "." || part === ".." || part.toLowerCase() === ".git"))) {
    throw new Error("better-dsh: paths must contain exact repository-relative file names");
  }
  const timeoutMs = raw.timeoutMs ?? 10_000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
    throw new Error("better-dsh: timeoutMs must be between 1 and 60000");
  }
  return { mode, effort, failureFeedback: raw.failureFeedback === true && mode === "stop",
    workspace: realpathSync(raw.workspace), paths: new Set(raw.paths),
    checks: raw.checks.map(({ name, command, feedbackHint }) => ({ name, command, feedbackHint })), timeoutMs };
}

function workspacePolicy(ctx, agent, workspace) {
  const policy = ctx.sandboxPolicy.resolve({ session: agent.session });
  if (!policy?.workspaceRoot || realpathSync(policy.workspaceRoot) !== workspace) {
    return undefined;
  }
  return policy;
}

async function shell(ctx, agent, config, signal, command, stdoutMaxBytes = 8_192) {
  const sandboxPolicy = workspacePolicy(ctx, agent, config.workspace);
  if (!sandboxPolicy) return { ok: false, failure: "sandbox-root-mismatch" };
  const spec = ctx.shell.resolve({
    command, workdir: config.workspace, signal, timeoutMs: config.timeoutMs, stdoutMaxBytes,
    sandboxPolicy,
  });
  const result = await (await ctx.shell.execute(spec)).result();
  const failure = result.timedOut ? "timeout" : result.aborted ? "aborted" :
    result.sandbox?.denied ? "denied" : result.signal ? "signal" :
    result.exitCode !== 0 ? "exit-code" : undefined;
  return {
    ok: failure === undefined, failure,
    text: result.stdout.text, truncated: result.stdout.truncated,
  };
}

async function snapshot(ctx, agent, config, signal, baseline) {
  const status = await shell(ctx, agent, config, signal,
    `${git} rev-parse --show-toplevel HEAD && ${git} status --porcelain=v1 -z --untracked-files=all --no-renames`,
    65_536);
  if (!status.ok) return skipped(`git-status-${status.failure}`);
  if (status.truncated) return skipped("status-too-large");
  const rootEnd = status.text.indexOf("\n");
  const headEnd = status.text.indexOf("\n", rootEnd + 1);
  if (rootEnd < 0 || headEnd < 0) return skipped("invalid-git-status");
  if (realpathSync(status.text.slice(0, rootEnd).replace(/\r$/u, "")) !== config.workspace) {
    return skipped("git-root-mismatch");
  }
  const head = status.text.slice(rootEnd + 1, headEnd).replace(/\r$/u, "");
  if (!/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/u.test(head)) return skipped("invalid-git-head");
  if (baseline && head !== baseline) return skipped("head-changed");
  const entries = status.text.slice(headEnd + 1).split("\0").filter(Boolean);
  if (!baseline) return entries.length ? skipped("dirty-start") : { head };
  if (!entries.length) return skipped("no-changes");
  if (entries.some((entry) => !/^[ M][ M] /u.test(entry))) return skipped("unsupported-change");
  const paths = entries.map((entry) => entry.slice(3)).sort();
  if (paths.some((path) => !config.paths.has(path))) return skipped("outside-file-scope");
  if (paths.some((path) => {
    const file = join(config.workspace, path);
    return !lstatSync(file).isFile() || realpathSync(file) !== file;
  })) return skipped("non-regular-file");
  const diff = await shell(ctx, agent, config, signal,
    `${git} diff --no-ext-diff --no-textconv --binary HEAD --`, 1_048_576);
  if (!diff.ok) return skipped(`git-diff-${diff.failure}`);
  if (diff.truncated) return skipped("diff-too-large");
  if (!diff.text) return skipped("no-diff");
  return {
    digest: createHash("sha256").update(status.text).update(diff.text).digest("hex"),
    paths,
  };
}

function unavailable(ctx, agent, config, signal) {
  if (signal.aborted) return "cancelled";
  if (realpathSync(ctx.workingDirectory.get(agent.session)) !== config.workspace) return "workdir-mismatch";
  if (!workspacePolicy(ctx, agent, config.workspace)) return "sandbox-root-mismatch";
  if (agent.inbox.nextStep.length || agent.inbox.nextTurn.length) return "pending-input";
  if (ctx.get("jobs")?.list(agent.session.id).some((job) =>
    job.owner === agent.session.id && (job.status === "running" || job.status === "stopping"))) return "active-job";
  return undefined;
}

function diagnostic(state, event, data) {
  try {
    writeSync(2, `better-dsh: ${JSON.stringify({
      version: 1, event, sessionId: state.agent.session.id, turn: state.turn, step: state.step, ...data,
    })}\n`);
    return true;
  } catch {
    return false;
  }
}

function logSkip(state, reason) {
  if (state.skips.has(reason)) return;
  state.skips.add(reason);
  diagnostic(state, "skipped", { reason });
}

function route(options, effort) {
  return options.provider === "deepseek-official" && options.model === "deepseek-flash" &&
    options.reasoningEffort === effort;
}

function summary(sessionId, paths, checks) {
  const files = paths.map((path) => `- ${JSON.stringify(path)}`).join("\n");
  const verification = checks.map(({ name, elapsedMs }) =>
    `- ${name}：通过（${(elapsedMs / 1000).toFixed(2)} 秒）`).join("\n");
  return [
    `Better DSH 已自动收尾。${checks.length} 项配置检查通过，工作区补丁在检查期间保持一致。`,
    "此摘要由 Better DSH 插件根据本地检查生成。",
    `修改文件\n${files}`,
    `检查结果\n${verification}`,
    `继续验证时，省略启用插件的补丁，并使用 --session-id ${sessionId} 提交后续请求。`,
  ].join("\n\n");
}

function failureNotice(check, hint) {
  const label = check.name.replace(/[\p{Cc}\p{Cf}]/gu, " ").slice(0, 72);
  const summary = `Better DSH 检查未通过：${label}`;
  const text = [
    summary + `（${check.failure}）。`,
    "请核对当前补丁，修复失败原因后再结束任务。",
    hint,
  ].filter(Boolean).join("\n");
  return {
    id: randomUUID(), role: "user", content: [{ type: "text", text }],
    source: { kind: "better-dsh-checkpoint", form: "notice", summary },
  };
}

export function apply(ctx, raw) {
  const config = configure(raw);
  if (!config) return;
  const sessions = new Map();

  ctx.on("agent/disposed", ({ agent }) => sessions.delete(agent.session.id));
  ctx.on("agent/pre-step", async ({ agent, turn, step, messages, modelSelection, signal }, next) => {
    const id = agent.session.id;
    let state = sessions.get(id);
    const firstStep = !state || state.turn !== turn;
    if (firstStep) {
      state = { agent, turn, firstStep: step, baseline: undefined, allowed: false, stop: undefined,
        checked: undefined, skips: new Set() };
      sessions.set(id, state);
    }
    state.step = step;
    state.signal = signal;
    state.allowed = false;
    state.stop = undefined;
    try {
      const reason = !route(modelSelection, config.effort) ? "unsupported-model-selection" :
        unavailable(ctx, agent, config, signal);
      if (reason) {
        logSkip(state, reason);
      } else if (firstStep) {
        const initial = await snapshot(ctx, agent, config, signal);
        if (initial.reason) logSkip(state, initial.reason);
        state.baseline = initial.head;
      } else if (messages.length) {
        logSkip(state, "pending-input");
      } else {
        state.allowed = Boolean(!state.observed && state.baseline && step > state.firstStep);
      }
    } catch {
      logSkip(state, "pre-step-error");
    }
    const decision = await next();
    if (decision.kind !== "enter" || !state.allowed) return decision;
    try {
      const previous = agent.session.deriveMessages().at(-1);
      if (previous?.role !== "tool" || unavailable(ctx, agent, config, signal)) return decision;
      const candidate = await evaluate(state, step, signal);
      if (!candidate) return decision;
      const { before, checks, passed } = candidate;
      if (passed) {
        if (config.mode === "observe") {
          diagnostic(state, "would-stop", { patchSha256: before.digest, paths: before.paths, checks });
          state.observed = true;
        } else {
          state.stop = candidate;
        }
      } else {
        const check = checks.at(-1);
        diagnostic(state, "check-failed", { patchSha256: before.digest, paths: before.paths, checks });
        if (config.failureFeedback && check.failure === "exit-code" && !state.feedbackGiven) {
          const message = failureNotice(check, config.checks[checks.length - 1].feedbackHint);
          state.feedbackGiven = true;
          diagnostic(state, "feedback-admitted", { source: "host", patchSha256: before.digest,
            check: check.name, messageId: message.id });
          return { ...decision, messages: [...decision.messages, message] };
        }
      }
    } catch {
      logSkip(state, "checkpoint-error");
    }
    return decision;
  });

  async function evaluate(state, step, signal) {
    const { agent, baseline } = state;
    const before = await snapshot(ctx, agent, config, signal, baseline);
    const reason = before.reason ?? unavailable(ctx, agent, config, signal);
    if (reason) {
      logSkip(state, reason);
      return undefined;
    }
    if (before.digest === state.checked) return undefined;
    const checks = [];
    for (const check of config.checks) {
      const start = performance.now();
      const result = await shell(ctx, agent, config, signal, check.command);
      checks.push({ name: check.name, passed: result.ok, elapsedMs: Math.round(performance.now() - start),
        ...(result.failure && { failure: result.failure }) });
      if (!result.ok) break;
    }
    const after = await snapshot(ctx, agent, config, signal, baseline);
    const blocked = after.reason ?? unavailable(ctx, agent, config, signal);
    if (blocked || after.digest !== before.digest) {
      logSkip(state, blocked ?? "patch-changed");
      return undefined;
    }
    if (sessions.get(agent.session.id) !== state || state.step !== step || state.signal !== signal) return undefined;
    state.checked = before.digest;
    return { before, checks, passed: checks.length === config.checks.length && checks.every((check) => check.passed) };
  }

  ctx.on("llm/stream", (options, next) => {
    const state = sessions.get(options.sessionId);
    const candidate = state?.stop;
    if (options.purpose !== undefined || !route(options, config.effort) || !candidate ||
        options.signal !== state.signal || !options.messages.some((message) => message.role === "tool")) return next();
    const { signal, step, baseline, agent } = state;
    return (async function* () {
      let text;
      try {
        const current = await snapshot(ctx, agent, config, signal, baseline);
        const blocked = current.reason ?? unavailable(ctx, agent, config, signal);
        if (blocked || current.digest !== candidate.before.digest) {
          logSkip(state, blocked ?? "patch-changed");
        } else if (sessions.get(options.sessionId) === state && state.step === step && state.stop === candidate) {
          state.stop = undefined;
          const { checks } = candidate;
          const logged = diagnostic(state, "stopped", { patchSha256: current.digest, paths: current.paths, checks, source: "host" });
          if (logged) text = summary(agent.session.id, current.paths, checks);
        }
      } catch {
        logSkip(state, "checkpoint-error");
        text = undefined;
      }
      if (text === undefined) {
        yield* next();
        return;
      }
      yield { type: "block-start", index: 0, blockType: "text" };
      yield { type: "text-delta", index: 0, text };
      yield { type: "block-end", index: 0, block: { type: "text", text } };
      yield { type: "finish", reason: { kind: "stop" } };
    })();
  });
}
