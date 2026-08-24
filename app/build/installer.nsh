!include "nsDialogs.nsh"
!include "LogicLib.nsh"

!ifndef BUILD_UNINSTALLER

Var Dialog
Var MediaInput
Var MediaURL
Var ApiKeyInput
Var ApiKey

!macro preInit
    StrCpy $INSTDIR "D:\\千里眼"
!macroend

Page custom configPageCreate configPageLeave

Function configPageCreate
    nsDialogs::Create 1018
    Pop $Dialog
    ${If} $Dialog == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 10u 100% 12u "MediaService 地址："
    Pop $0
    ${NSD_CreateText} 0 26u 100% 14u "http://10.0.20.219:22222"
    Pop $MediaInput

    ${NSD_CreateLabel} 0 54u 100% 12u "客户端 API Key："
    Pop $0
    ${NSD_CreatePassword} 0 70u 100% 14u "Yx7pK4vN9mQ2tR8wF6cH3sD5jL1aZ0eB"
    Pop $ApiKeyInput

    ${NSD_CreateLabel} 0 100u 100% 30u "客户端将从 MediaService 自动获取 RTMP 推流地址；API Key 必须与服务端 CLIENT_API_KEY 一致。"
    Pop $0
    nsDialogs::Show
FunctionEnd

Function configPageLeave
    ${NSD_GetText} $MediaInput $MediaURL
    ${NSD_GetText} $ApiKeyInput $ApiKey
    ${If} $MediaURL == ""
    ${OrIf} $ApiKey == ""
        MessageBox MB_ICONEXCLAMATION "MediaService 地址和 API Key 均为必填。"
        Abort
    ${EndIf}
FunctionEnd

!macro customInstall
    FileOpen $0 "$INSTDIR\config.json" w
    ${IfNot} ${Errors}
        FileWrite $0 '{$\"mediaServiceURL$\":$\"$MediaURL$\",$\"apiKey$\":$\"$ApiKey$\"}'
        FileClose $0
    ${EndIf}
!macroend

!endif
