/* 部署模式开关：
   - "server"：本地/自托管 FastAPI（读 /api/*，由 server.py 提供）
   - "static"：纯静态部署（读 /data/*.json，由 GitHub Actions 数据管线维护）
   静态部署时 build_dist.py 会用 mode:"static" 的副本覆盖本文件。 */
window.FANREN = { mode: "server" };
