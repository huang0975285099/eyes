!include "nsDialogs.nsh"
!include "LogicLib.nsh"

!ifndef BUILD_UNINSTALLER

Var Dialog
Var HostInput
Var HostURL
Var RecordingInput
Var RecordingURL
Var SrsInput
Var SrsHost
Var ApiKeyInput
Var ApiKey

Page custom configPageCreate configPageLeave

Function configPageCreate
    nsDialogs::Create 1018
    Pop $Dialog
    ${If} $Dialog == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 12u "公网中心 API 地址："
    Pop $0
    ${NSD_CreateText} 0 14u 100% 14u "http://112.18.238.6:52351"
    Pop $HostInput

    ${NSD_CreateLabel} 0 36u 100% 12u "RecordingService 地址："
    Pop $0
    ${NSD_CreateText} 0 50u 100% 14u "http://10.0.20.219:8089"
    Pop $RecordingInput

    ${NSD_CreateLabel} 0 72u 100% 12u "SRS RTMP 地址："
    Pop $0
    ${NSD_CreateText} 0 86u 100% 14u "10.0.20.219:21935"
    Pop $SrsInput

    ${NSD_CreateLabel} 0 108u 100% 12u "客户端 API Key："
    Pop $0
    ${NSD_CreatePassword} 0 122u 100% 14u ""
    Pop $ApiKeyInput

    ${NSD_CreateLabel} 0 146u 100% 24u "以上配置由管理员提供；API Key 必须与 RecordingService 的 CLIENT_API_KEY 一致。"
    Pop $0
    nsDialogs::Show
FunctionEnd

Function configPageLeave
    ${NSD_GetText} $HostInput $HostURL
    ${NSD_GetText} $RecordingInput $RecordingURL
    ${NSD_GetText} $SrsInput $SrsHost
    ${NSD_GetText} $ApiKeyInput $ApiKey
    ${If} $HostURL == ""
    ${OrIf} $RecordingURL == ""
    ${OrIf} $SrsHost == ""
    ${OrIf} $ApiKey == ""
        MessageBox MB_ICONEXCLAMATION "所有配置项均为必填。"
        Abort
    ${EndIf}
FunctionEnd

!macro customInstall
    FileOpen $0 "$INSTDIR\config.json" w
    ${IfNot} ${Errors}
        FileWrite $0 '{$\"hostURL$\":$\"$HostURL$\",$\"recordingServiceURL$\":$\"$RecordingURL$\",$\"srsHost$\":$\"$SrsHost$\",$\"apiKey$\":$\"$ApiKey$\"}'
        FileClose $0
    ${EndIf}
!macroend

!endif
