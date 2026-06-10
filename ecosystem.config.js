module.exports = {
  apps: [
    {
      name: "outbound-server",
      script: "python3",
      args: "-m uvicorn server:app --host 0.0.0.0 --port 8000",
      cwd: "/home/bhavik/Bhavik-AI-Voice-Agent",
      interpreter: "none",
      env_file: "/home/bhavik/Bhavik-AI-Voice-Agent/.env",
      restart_delay: 3000,
      max_restarts: 10,
      autorestart: true,
    },
    {
      name: "outbound-agent",
      script: "python3",
      args: "agent.py start",
      cwd: "/home/bhavik/Bhavik-AI-Voice-Agent",
      interpreter: "none",
      env_file: "/home/bhavik/Bhavik-AI-Voice-Agent/.env",
      restart_delay: 5000,
      max_restarts: 10,
      autorestart: true,
    }
  ]
}
