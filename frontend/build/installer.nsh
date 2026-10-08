; electron-builder NSIS 钩子
; - customInit：备份旧 settings.json（解压前，时机在 .onInit 末尾）
; - customInstall：恢复备份 + 写注册表 + 清临时（解压后）
; - customUnInstall：删注册表 + 硬删 Electron userData（保留 dc29cac）

!macro customInit
  ; #598 备份段：NSIS 启动时（.onInit 末尾调用，在 uninstallOldVersion + 解压新版本之前）
  ; - $INSTDIR 已是"将安装到"的位置：全新安装 = 用户选择；重装 = 旧 INSTDIR（从注册表读）
  ; - 此时旧 INSTDIR 还在（旧 uninstaller 还没跑），旧 settings.json 可读
  ; - 备份到 $TEMP\ShortVideoToolBackup\ 跨解压保留（NSIS installer 进程持续到结束）
  ; - 路径：旧 INSTDIR\resources\backend\config\settings.json
  ; - 首次安装（settings.json 不存在）→ 静默跳过
  ; - 旧版（1.0.1 之前）settings.json 路径可能不存在（应用目录 config/ 概念引入时间不同）→ 静默跳过
  ;
  ; #598 路径硬编码说明：依赖 PyInstaller onedir 布局（resources/backend/）。
  ; package.json extraResources 复制 ../backend/dist 到 resources/backend/。
  ; 如果未来改 onefile（单 exe）或改 backend 目录结构 → 备份/恢复路径失效需同步改。
  ;
  ; CopyFiles 失败行为：NSIS 3.x CopyFiles 失败时**只写错误日志不弹对话框**，
  ; 安装继续到下一步（不像 File 指令会弹错误）。本段是 best-effort，失败不阻塞安装。
  ; 加 SetOverwrite on 显式声明：备份文件已存在时（如重装前残留）覆盖。
  CreateDirectory "$TEMP\ShortVideoToolBackup"
  SetOverwrite on
  IfFileExists "$INSTDIR\resources\backend\config\settings.json" 0 skip_backup
    CopyFiles /SILENT "$INSTDIR\resources\backend\config\settings.json" "$TEMP\ShortVideoToolBackup\settings.json"
  skip_backup:
!macroend

!macro customInstall
  ; #598 恢复段：NSIS 解压新版本到 $INSTDIR 之后调用
  ; - 从 $TEMP\ShortVideoToolBackup\settings.json 恢复到新 $INSTDIR\resources\backend\config\
  ; - 写注册表：当前 InstallPath + Version（让下次重装可读）
  ; - 清临时备份目录
  ; 恢复后 settings.json 包含旧 data_dir 字段 → 后端 check_data_dir 启动时直接命中 → 不弹数据目录选择框
  ; CopyFiles 失败容错：NSIS 3.x 失败只写日志不弹对话框，best-effort 失败不阻塞安装。
  IfFileExists "$TEMP\ShortVideoToolBackup\settings.json" 0 skip_restore
    CreateDirectory "$INSTDIR\resources\backend\config"
    SetOverwrite on
    CopyFiles /SILENT "$TEMP\ShortVideoToolBackup\settings.json" "$INSTDIR\resources\backend\config\settings.json"
  skip_restore:

  ; 写注册表（HKCU 不需要管理员权限，重装/升级时用相同用户身份保持一致）
  ; 路径在 NSIS Unicode 模式下自动处理中文（如 "C:\Program Files\短视频工具\"）
  WriteRegStr HKCU "Software\ShortVideoTool" "InstallPath" "$INSTDIR"
  WriteRegStr HKCU "Software\ShortVideoTool" "Version" "${VERSION}"

  ; 清临时备份（成功恢复后清理；保留仅供排查）
  RMDir /r "$TEMP\ShortVideoToolBackup"
!macroend

!macro customUnInstall
  ; #598 删注册表 + 保留 dc29cac 硬删 Electron userData
  ; - 删注册表：HKCU\Software\ShortVideoTool（InstallPath + Version）
  ; - 硬删 userData：不显示勾选框（vs electron-builder deleteAppDataOnUninstall）
  ; - 数据目录（<盘根>/ShortVideoToolData/）NSIS 不知道在哪，自动安全保留
  ; - 若目录不存在则跳过（NSIS 静默忽略更稳，IfFileExists 仅消除日志噪音）
  DeleteRegKey HKCU "Software\ShortVideoTool"

  SetShellVarContext current
  IfFileExists "$APPDATA\shortvideo-tool" 0 +2
    RMDir /r "$APPDATA\shortvideo-tool"
!macroend
