; Inno Setup 6 脚本: 本地屏译安装包

#define MyAppName "本地屏译"
#define MyAppVersion "2.2.0"
#define MyAppPublisher "本地屏译"
#define MyAppURL "https://github.com/2061863797/LocalScreenTranslator"
#define MyAppExeName "LocalScreenTranslator.exe"
#ifndef AppSourceDir
  #define AppSourceDir "dist\LocalScreenTranslator"
#endif

[Setup]
AppId={{D37E8873-5F79-4B52-8729-183602D58E91}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=LocalScreenTranslator-Setup
SetupIconFile=icon.ico
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes

; 明确启用安装目录选择页面，不强制沿用旧目录，让用户自由选择安装盘符与路径
DisableDirPage=no
UsePreviousAppDir=no
AlwaysShowDirOnReadyPage=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Dirs]
Name: "{app}\runtime\models"

[Files]
; 打包后的主程序与 Python 运行时（onedir 产物）
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Excludes: "runtime\*"; Flags: ignoreversion recursesubdirs createallsubdirs
; 进程内翻译运行资源：固定版本 llama.cpp 动态库
Source: "runtime\llama-native\*"; DestDir: "{app}\runtime\llama-native"; Flags: ignoreversion recursesubdirs createallsubdirs
; PP-OCRv6 ONNX 模型与清单
Source: "runtime\ocr\*"; DestDir: "{app}\runtime\ocr"; Flags: ignoreversion recursesubdirs createallsubdirs
; 翻译模型目录：默认 Model-Free 解耦分发；仅当显式指定 IncludeModels=1 时打包模型文件
#ifdef IncludeModels
  #if IncludeModels == "1"
    Source: "runtime\models\*"; DestDir: "{app}\runtime\models"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
  #endif
#endif
; 基础附属文件与许可证
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "NOTICE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
; 程序设置中开启的开机自启动项：安装时不创建，卸载时一并删除
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "LocalScreenTranslator"; ValueType: none; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
