module.exports = {
  apps: [{
    name: "monitor-esame-camorino",
    script: "monitor_esame.py",
    interpreter: "venv/bin/python3",
    cwd: __dirname,
    autorestart: true,
    max_restarts: 20,
    restart_delay: 5000,
  }],
};
