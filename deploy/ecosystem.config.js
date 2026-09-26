// PM2 app for Dialyn: `pm2 startOrReload deploy/ecosystem.config.js`
// Listens on 127.0.0.1 only; Nginx publishes it over HTTPS.
const fs = require("fs");
const path = require("path");

function dialynPort() {
  try {
    const line = fs
      .readFileSync(path.join(__dirname, ".env"), "utf8")
      .split("\n")
      .find((l) => l.startsWith("DIALYN_PORT="));
    if (line) return line.split("=")[1].trim();
  } catch (_) {}
  return "7860";
}

module.exports = {
  apps: [
    {
      name: "dialyn",
      cwd: path.join(__dirname, "..", "agent"),
      script: ".venv/bin/uvicorn",
      args: `app.main:app --host 127.0.0.1 --port ${dialynPort()} --proxy-headers --forwarded-allow-ips 127.0.0.1`,
      interpreter: "none",
      autorestart: true,
      max_memory_restart: "1500M",
      kill_timeout: 10000,
      time: true,
    },
  ],
};
