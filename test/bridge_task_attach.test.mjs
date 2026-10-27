// scripts/task_attach.mjs 的回归测试（不需要浏览器 / 不需要 Playwright）。
//   node --test test/bridge_task_attach.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { attachTaskViaApi, taskCreateBodies, taskAttachShapes } from "../scripts/task_attach.mjs";

const MEETING = { id: "sess-uuid-1", startMs: 1790842500000 };

/**
 * 假内部 API：`behavior` 决定服务端「认哪种 body」。
 *   server.postAcceptsAttachments  —— POST /tasks 直接收 attachments
 *   server.attachEndpoint          —— /tasks/attach 收哪种 payload key
 */
function fakeApi(behavior) {
  const state = { tasks: [...(behavior.existing || [])], calls: [], deleted: [] };
  const callApi = async (method, path, body) => {
    state.calls.push([method, path, body]);
    if (method === "POST" && path === "/tasks") {
      if (behavior.postFails && behavior.postFails(body)) {
        return { status: 400, text: "bad request", json: null };
      }
      const att = behavior.postAcceptsAttachments && body.attachments
        ? body.attachments
        : [];
      const task = { id: "task-" + (state.tasks.length + 1), title: body.title, attachments: att };
      state.tasks.push(task);
      return { status: 201, json: { id: task.id, title: task.title, attachments: att } };
    }
    if (method === "POST" && path === "/tasks/attach") {
      if (body.taskId && behavior.attachKey === "taskId,attachments") {
        const t = state.tasks.find((x) => x.id === body.taskId);
        if (t) t.attachments = body.attachments;
        return { status: 200, json: {} };
      }
      return { status: 400, text: "unexpected payload", json: null };
    }
    if (method === "DELETE" && path.startsWith("/tasks/")) {
      const id = decodeURIComponent(path.slice("/tasks/".length));
      state.tasks = state.tasks.filter((t) => t.id !== id);
      state.deleted.push(id);
      return { status: 200, json: {} };
    }
    return { status: 404, text: "no route " + path, json: null };
  };
  const tasksForMeeting = async (id) =>
    state.tasks.filter((t) => (t.attachments || []).some((a) => a.externalId === id));
  const deleteTask = async (id) => {
    const r = await callApi("DELETE", "/tasks/" + encodeURIComponent(id), {});
    return r;
  };
  return { state, deps: { callApi, tasksForMeeting, deleteTask } };
}

test("POST /tasks 直接收 attachments：一次就成", async () => {
  const { state, deps } = fakeApi({ postAcceptsAttachments: true });
  const out = await attachTaskViaApi(deps, MEETING, "写论文", "先写 intro");
  assert.equal(out.task.title, "写论文");
  assert.equal(out.task.attachments[0].externalId, MEETING.id);
  assert.equal(state.calls.filter((c) => c[0] === "POST").length, 1);
  assert.deepEqual(state.deleted, []);
});

test("POST 不收 attachments：退到 /tasks/attach 也能成", async () => {
  const { state, deps } = fakeApi({ attachKey: "taskId,attachments" });
  const out = await attachTaskViaApi(deps, MEETING, "写论文", "");
  assert.equal(out.task.attachments[0].externalId, MEETING.id);
  assert.ok(state.calls.some((c) => c[1] === "/tasks/attach"));
  assert.ok(out.attempts.some((a) => a.includes("/tasks/attach") && a.includes("HTTP 200")),
    "attach 那一步要记进 attempts，方便排查");
  assert.ok(out.via.includes("/tasks/attach"), "via 要说明实际走的是哪条路");
});

test("这一场已经挂着别的 task：照样能再挂一个（用户报的 bug）", async () => {
  const { state, deps } = fakeApi({
    postAcceptsAttachments: true,
    existing: [{ id: "old-1", title: "旧任务", attachments: [{ type: "session", externalId: MEETING.id }] }],
  });
  const out = await attachTaskViaApi(deps, MEETING, "新任务", "");
  assert.equal(out.task.title, "新任务");
  const attached = await deps.tasksForMeeting(MEETING.id);
  assert.deepEqual(attached.map((t) => t.title).sort(), ["新任务", "旧任务"]);
  assert.equal(state.deleted.length, 0);
});

test("建出来了但挂不上：回滚删除 + 报错里带全部尝试", async () => {
  const { state, deps } = fakeApi({ attachKey: "never-matches" });
  await assert.rejects(
    () => attachTaskViaApi(deps, MEETING, "挂不上", ""),
    (e) => {
      assert.ok(/没能挂到这场 session 上/.test(e.message));
      assert.ok(/回滚/.test(e.message));
      assert.deepEqual(e.attempts.length > 0, true);
      return true;
    }
  );
  assert.deepEqual(state.deleted, ["task-1"]);
  assert.deepEqual(await deps.tasksForMeeting(MEETING.id), []);
});

test("连 POST /tasks 都失败：直接报错，不删任何东西", async () => {
  const { state, deps } = fakeApi({ postFails: () => true });
  await assert.rejects(() => attachTaskViaApi(deps, MEETING, "写论文", ""), /建 task 的 API 都失败了/);
  assert.deepEqual(state.deleted, []);
});

test("候选 body / 端点形状：由简到繁，且都带 attachments", () => {
  const bodies = taskCreateBodies("t", "n", MEETING.id);
  assert.equal(bodies[0][1].attachments[0].externalId, MEETING.id);
  const shapes = taskAttachShapes("task-1", MEETING.id, { id: "task-1", attachments: [] });
  assert.ok(shapes.length >= 4);
  for (const [, , , body] of shapes) {
    assert.ok(JSON.stringify(body).includes(MEETING.id), "每个候选都得指向这场 session");
  }
  assert.deepEqual(taskAttachShapes(null, MEETING.id, {}), []);
});
