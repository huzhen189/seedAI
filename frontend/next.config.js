/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // 开发态:把 /api 代理到业务服务(容器名 business / 本地 :8000)
  async rewrites() {
    const target = process.env.BUSINESS_API_URL || "http://localhost:8000";
    return [{ source: "/api/:path*", destination: `${target}/api/:path*` }];
  },
};

module.exports = nextConfig;
