/**
 * task_attach.mjs —— 「给 session 挂 task」的纯逻辑（不碰浏览器，方便测试）。
 *
 * 为什么存在
 * ----------
 * 这一场 session **已经有 task** 时，Focusmate 的右侧面板里就没有 "Add task"
 * 了（UI 只给一次入口），DOM 路径必然失败 —— 用户报的
 * 「已经有 task 的时候无法加入额外的 task」就是这么来的。
 * 内部 API 没这个限制（task ↔ session 是 n:n，`attachments` 是数组），
 * 所以默认走 API，DOM 只当兜底。
 *
 * 但 `POST /v1/tasks` 的 body 形状没有公开文档，attach 端点（`/tasks/attach`）
 * 也一样。所以这里按「由简到繁」试几种候选形状，**每一种都用 GET /tasks 复核**
 * 「title 一致 + attachment 指向这场」才算数；建出来了却没挂上的孤儿任务删掉，
 * 不在 Task Manager 里留垃圾。
 *
 * bridge.mjs 只负责把真实的 callApi / tasksForMeeting / deleteTask 传进来。
 */

/** POST /tasks 的候选 body（由简到繁）。 */
export function taskCreateBodies(title, notes, meetingId) {
  const att = [{ type: "session", externalId: meetingId }];
  const base = notes ? { title, description: notes } : { title };
  return [
    ["POST /tasks {title,description?,attachments}", { ...base, attachments: att }],
    ["POST /tasks {title,description?,reusable,attachments}", { ...base, reusable: false, attachments: att }],
    ["POST /tasks {title,description?}", base],
  ];
}

/** 挂载（attach）候选：[label, method, path, body]。`id` 为空时返回空数组。 */
export function taskAttachShapes(id, meetingId, created) {
  if (!id) return [];
  const att = [{ type: "session", externalId: meetingId }];
  const enc = encodeURIComponent(id);
  return [
    ["POST /tasks/attach {taskId,attachments}", "POST", "/tasks/attach", { taskId: id, attachments: att }],
    ["POST /tasks/attach {taskId,attachment}", "POST", "/tasks/attach", { taskId: id, attachment: att[0] }],
    ["POST /tasks/attach {id,attachments}", "POST", "/tasks/attach", { id, attachments: att }],
    ["POST /tasks/attach {task:{id},attachments}", "POST", "/tasks/attach", { task: { id }, attachments: att }],
    ["POST /tasks/" + enc + "/attach {attachments}", "POST", "/tasks/" + enc + "/attach", { attachments: att }],
    ["PUT /tasks/" + enc + " {…,attachments}", "PUT", "/tasks/" + enc,
      { ...((created && created.task) || created || {}), attachments: att }],
  ];
}

/** 从 POST /tasks 的响应里挖 task id（服务端形状未知，见到哪个算哪个）。 */
export function taskIdOf(json) {
  if (!json) return null;
  return json.id || (json.task && json.task.id) || (json.data && json.data.id) || null;
}

const okStatus = (s) => s === 200 || s === 201 || s === 204;

/**
 * 用 API 建 task 并挂到 `meeting` 上，返回**复核过**的 task（形状同 GET /tasks 的一条）。
 * 任何一步失败都抛 Error（`attempts` 里是全部尝试记录），且不留孤儿任务。
 *
 * @param {object} deps  { callApi(method,path,body), tasksForMeeting(id), deleteTask(id) }
 */
export async function attachTaskViaApi(deps, meeting, title, notes) {
  const { callApi, tasksForMeeting, deleteTask } = deps;
  const attempts = [];
  const fail = (msg) => {
    const e = new Error(msg);
    e.attempts = attempts;
    return e;
  };
  const findAttached = async () => {
    try {
      const rows = await tasksForMeeting(meeting.id);
      return rows.find((t) => t.title === title) || null;
    } catch (e) {
      attempts.push("复核失败：" + brief(e));
      return null;
    }
  };

  let created = null;
  let createdVia = null;
  for (const [label, body] of taskCreateBodies(title, notes, meeting.id)) {
    const r = await callApi("POST", "/tasks", body);
    if (okStatus(r.status) && r.status !== 204) {
      created = r.json || {};
      createdVia = label;
      attempts.push(label + " → HTTP " + r.status);
      break;
    }
    attempts.push(label + " → HTTP " + r.status + " " + String(r.text || "").slice(0, 160));
  }
  if (!created) throw fail("建 task 的 API 都失败了（" + attempts.join("；") + "）");

  const id = taskIdOf(created);
  // 有的实现 POST 时就把 attachment 收下了 —— 先看一眼，别多做无用功。
  const direct = await findAttached();
  if (direct) return { task: direct, attempts, via: createdVia };

  for (const [label, method, path, body] of taskAttachShapes(id, meeting.id, created)) {
    const r = await callApi(method, path, body);
    if (!okStatus(r.status)) {
      attempts.push(label + " → HTTP " + r.status + " " + String(r.text || "").slice(0, 160));
      continue;
    }
    attempts.push(label + " → HTTP " + r.status);
    const found = await findAttached();
    if (found) return { task: found, attempts, via: createdVia + " + " + label };
    attempts.push("复核：GET /tasks 里还没看到 attachment");
  }

  // 建出来了但没挂上：删掉，别在 Task Manager 里留垃圾。
  if (id && deleteTask) {
    try {
      await deleteTask(id);
      attempts.push("回滚：已删除孤儿 task " + id);
    } catch (e) {
      attempts.push("回滚失败：" + brief(e));
    }
  }
  throw fail(
    "task 建出来了（" + createdVia + "）但没能挂到这场 session 上" +
      (id ? "（id=" + id + "，已尝试回滚）" : "") + "。试过：" + attempts.join("；")
  );
}

function brief(e) {
  return String((e && e.message) || e).replace(/\s+/g, " ").slice(0, 200);
}
