/* 凡人道盘 · Cloudflare Worker 入口
   静态资产由 wrangler 直接分发；/data/* 实时代理 GitHub raw（仓库 data/site/ 目录），
   边缘缓存 5 分钟——GitHub Actions 提交新数据后，线上最多 5 分钟内生效，无需重新部署。
   回落顺序：边缘缓存 → GitHub raw → 部署包内静态副本（旧数据好过报错）。 */

const GH_RAW_BASE =
  "https://raw.githubusercontent.com/the-beating-light-of-the-nail/fanren-dao-pan/main/data/site";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/data/derived.json" || url.pathname.startsWith("/data/eps/")) {
      return proxyData(url.pathname, request, env, ctx);
    }
    return env.ASSETS.fetch(request);
  },
};

async function proxyData(path, request, env, ctx) {
  const cache = caches.default;
  const hit = await cache.match(request, { ignoreMethod: true });
  if (hit) return hit;
  try {
    const upstream = await fetch(GH_RAW_BASE + path.slice("/data".length));
    if (!upstream.ok) throw new Error("upstream " + upstream.status);
    const resp = new Response(upstream.body, upstream);
    resp.headers.set("Content-Type", "application/json; charset=utf-8");
    resp.headers.set("Cache-Control", "public, max-age=300");
    ctx.waitUntil(cache.put(request, resp.clone()));
    return resp;
  } catch (e) {
    const fallback = await env.ASSETS.fetch(request);
    if (fallback.ok) return fallback;
    return new Response("data unavailable", { status: 502 });
  }
}
