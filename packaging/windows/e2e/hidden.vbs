Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File"
For Each a In WScript.Arguments
  cmd = cmd & " """ & a & """"
Next
WScript.Quit sh.Run(cmd, 0, True)
