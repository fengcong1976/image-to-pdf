; 图片转PDF工具 · 安装包脚本（Inno Setup 6）
;
; 编译：先运行 build_exe.bat 生成 dist\图片转PDF工具\，再执行
;   "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
; 产物：dist\安装包\图片转PDF工具-1.3.0-安装包.exe
;
; 关键一条：PrivilegesRequired=lowest
; 装到 %LOCALAPPDATA%\Programs\图片转PDF工具，注册表只写 HKCU，
; 全程不请求管理员权限，所以安装时不会弹 UAC，也不需要用户点「是」。
;
; 和「视频文件压缩」那份的区别：没有随包的 ffmpeg，整个 _internal 就是
; Python 运行时 + Qt + OpenCV + PyMuPDF + Pillow，装好即可用，不依赖系统环境。

#define MyAppName "图片转PDF工具"
#define MyAppVersion "1.3.1"
#define MyAppPublisher "深圳市创炫互动网络科技有限公司"
#define MyAppURL "https://www.mrheedu.com"
#define MyAppExeName "图片转PDF工具.exe"

[Setup]
; AppId 一旦发布就不能改，否则新版本会被当成另一个软件、无法覆盖升级
AppId={{7C4A1B92-3D61-4E7A-9C2B-5F8E0A6D4B13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion=1.3.0.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}

; 每个用户装到自己的目录：无管理员权限也能装，不弹 UAC
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

; Windows 10 22H2 / 11 的 RedirectionGuard 缓解会阻止安装程序遍历「由非管理员
; 创建的 junction」，若用户的 %LOCALAPPDATA%\Programs 被联接到别的盘，全新安装
; 就会在「正在创建目录」时失败：错误 448「无法遍历路径，因为它包含不受信任的装入点」。
; 本程序是 PrivilegesRequired=lowest 的每用户安装，{app} 本就是用户自己可写的
; 目录，RedirectionGuard 要防的「借公共可写目录 + 联接点提权」在这里不成立，
; 因此关闭该缓解，保证装在 junction 下的机器也能全新安装。
; 命令行也可用 /NOREDIRECTIONGUARD 覆盖，但那需要用户手动加参数，不如这里一次解决。
RedirectionGuard=no

; 程序是 64 位（Python 3.13 x64），32 位系统直接挡掉
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 整个文件夹约 255MB（OpenCV 82MB / Qt 43MB / PyMuPDF 38MB / HEIC 22MB）
ExtraDiskSpaceRequired=0
OutputDir=installer_output
OutputBaseFilename={#MyAppName}-{#MyAppVersion}-安装包
SetupIconFile=resources\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装前若程序正在运行，自动结束它，免得文件占用装不上
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "dist\{#MyAppName}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyAppName}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载后残留的空目录一并清掉（用户自己放进来的文件不动）
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"
