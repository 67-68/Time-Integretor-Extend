// 只读 + 删除：通过内部 API 列/删 task（用来清理 add_task 的测试数据）。
//   node test/tasks_api.mjs list
//   node test/tasks_api.mjs delete <taskId>
import { openSession, getToken, api, log } from "../scripts/_lib.mjs";

const cmd = process.argv[2] || "list";
const { ctx, page } = await openSession({ headless: true });
try {
  const token = await getToken(page);
  if (cmd === "list") {
    const r = await api(page, { path: "/tasks", token });
    log(`GET /tasks → ${r.status}`);
    const tasks = r.json?.tasks ?? r.json ?? [];
    const rows = (Array.isArray(tasks) ? tasks : []).map((t) => ({
      id: t.id,
      title: t.title,
      description: t.description,
      completedAt: t.completedAt,
      attachments: (t.attachments || []).map((a) => `${a.type || a.attachmentType}:${a.externalId}`),
    }));
    console.log(JSON.stringify(rows.slice(-15), null, 1));
  } else if (cmd === "delete") {
    const id = process.argv[3];
    if (!id) throw new Error("需要 taskId");
    const r = await api(page, { method: "DELETE", path: `/tasks/${encodeURIComponent(id)}`, token, body: {} });
    log(`DELETE /tasks/${id} → ${r.status} ${r.text.slice(0, 200)}`);
  } else {
    throw new Error(`未知命令 ${cmd}`);
  }
} finally {
  await ctx.close().catch(() => {});
}
process.exit(0);
