# watch.py 로 떠 있는 pythonw 프로세스만 골라서 종료
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*watch.py*' } |
  ForEach-Object { Write-Host "종료: PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
