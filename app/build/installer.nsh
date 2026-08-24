!include "nsDialogs.nsh"
!include "LogicLib.nsh"

!ifndef BUILD_UNINSTALLER

Var Dialog
Var MediaInput
Var MediaURL

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
    ${NSD_CreateText} 0 26u 100% 14u "http://112.18.238.6:18888"
    Pop $MediaInput

    ${NSD_CreateLabel} 0 54u 100% 30u "客户端将从 MediaService 自动获取 RTMP 推流地址。"
    Pop $0
    nsDialogs::Show
FunctionEnd

Function configPageLeave
    ${NSD_GetText} $MediaInput $MediaURL
    ${If} $MediaURL == ""
        MessageBox MB_ICONEXCLAMATION "MediaService 地址为必填。"
        Abort
    ${EndIf}
FunctionEnd

!macro customInstall
    FileOpen $0 "$INSTDIR\config.json" w
    ${IfNot} ${Errors}
        FileWrite $0 '{$\"mediaServiceURL$\":$\"$MediaURL$\"}'
        FileClose $0
    ${EndIf}
!macroend

!endif
