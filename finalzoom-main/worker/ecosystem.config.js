// PM2 ecosystem config — auto-restart, RAM cap, low-latency restart on crash.
// Use:
//   npm i -g pm2
//   pm2 start ecosystem.config.js
//   pm2 save && pm2 startup           # persist on reboot
//
// This pairs with the architecture doc's "PM2 Recommended Config".
module.exports = {
  apps: [
    {
      name: "zoom-worker-pool",
      cwd: __dirname,
      // launch via xvfb-run on Linux; falls back to direct python on Windows
      script: process.platform === "linux"
        ? "/bin/bash"
        : "python",
      args: process.platform === "linux"
        ? ["-c", "source ./start_xvfb.sh && exec python zoom_worker_pool.py"]
        : ["zoom_worker_pool.py"],
      interpreter: "none",
      instances: 1,                   // 1 worker per RDP — pool handles concurrency
      exec_mode: "fork",
      autorestart: true,
      max_memory_restart: "2G",       // restart if memory leak makes worker > 2 GB
      restart_delay: 5000,
      kill_timeout: 15000,
      max_restarts: 50,
      env: {
        NODE_ENV: "production",
        // Pass-through; .env is the real source of truth
        DISPLAY: ":99",
      },
      out_file: "./logs/worker.out.log",
      error_file: "./logs/worker.err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};
