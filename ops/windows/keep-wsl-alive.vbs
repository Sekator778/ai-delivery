' WSL-AiDelivery-KeepAlive launcher (hidden)
'
' Purpose: keep the Ubuntu WSL distribution alive across the Windows session so that
' systemd services (ai-delivery-windmill.service, claude-tg-bot.service) and Docker
' containers do not stop when there is no interactive shell open.
'
' Why VBS: schtasks runs wsl.exe in Interactive mode by default, which opens a visible
' conhost.exe window. wscript.exe is a GUI host that can launch processes hidden
' (WindowStyle = 0) and exits immediately, leaving the WSL session running in background.
'
' Deploy to: C:\Users\user\AppData\Local\AI-Delivery\keep-wsl-alive.vbs
' Task Scheduler: wscript.exe "C:\Users\user\AppData\Local\AI-Delivery\keep-wsl-alive.vbs"

CreateObject("WScript.Shell").Run "wsl.exe -d Ubuntu --exec /bin/sh -c ""sleep infinity""", 0, False
