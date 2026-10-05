; electron-builder NSIS 钩子
; - install：安装时（保留可扩展）
; - uninstall：硬删 Electron userData（不显示勾选框）

!macro customInstall
  ; 占位：当前无安装期自定义动作
!macroend

!macro customUnInstall
  ; 硬删 Electron userData：
  ; - app.getPath('appData') = %APPDATA%\<package.json#name>（小写 shortvideo-tool）
  ; - 不显示勾选框，强制删除
  ; - 数据目录（<盘根>/ShortVideoToolData/）NSIS 不知道在哪，自动安全保留
  ; - 若目录不存在则跳过（NSIS 静默忽略更稳，IfFileExists 仅消除日志噪音）
  SetShellVarContext current
  IfFileExists "$APPDATA\shortvideo-tool" 0 +2
    RMDir /r "$APPDATA\shortvideo-tool"
!macroend
