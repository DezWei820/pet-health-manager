@echo off
rem Redis 一键启动（右键以管理员身份运行）：优先 Windows 服务，服务起不来则手动进程
net start Redis 2>nul || start "" "C:\Users\weidezhao\tools\redis\redis-server.exe" --port 6379
echo Redis 已启动，端口 6379
pause
