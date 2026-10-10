import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the dashboard on its own port and forwards API calls to
// a gateway running in the local cluster. The gateway only accepts
// state-changing requests whose Origin is its own address, so the proxy
// presents that Origin on the dashboard's behalf.
const gateway = process.env.GATEWAY_URL ?? "http://gateway.localhost";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: gateway, changeOrigin: true, headers: { origin: gateway } },
    },
  },
});
