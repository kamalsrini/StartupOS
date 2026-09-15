/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Self-contained server bundle for the Docker image (Dockerfile.web copies .next/standalone).
  output: "standalone",
  env: {
    // Baked in at BUILD time. "/api" (relative, same origin) in the deployed single-hostname topology;
    // an absolute origin for local dev against `make dev-api`. web/lib/api.ts handles both.
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
  },
  // No `/` -> `/cockpit` redirect: app/page.tsx does that decision itself, so a signed-out visitor goes to
  // /login directly instead of loading the cockpit first and being bounced out of it (Sprint 3d, Track G).
};

export default nextConfig;
