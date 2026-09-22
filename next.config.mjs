/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The Python function under /api serves the data; Next.js only renders the UI.
  // No rewrites here -- vercel.json owns /api/* routing so it works identically
  // in `vercel dev` and in production.
  eslint: { ignoreDuringBuilds: true },
};

export default nextConfig;
