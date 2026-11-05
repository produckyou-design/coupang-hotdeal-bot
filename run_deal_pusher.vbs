Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Project\coupang"
WshShell.Run """C:\Users\user\AppData\Local\Programs\Python\Python312\pythonw.exe"" push_deals.py", 0, False
