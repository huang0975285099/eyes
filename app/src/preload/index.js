import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('eyesAPI', {
    getStatus: () => ipcRenderer.invoke('app:get-status'),
    setUserName: (value) => ipcRenderer.invoke('device:set-user-name', value),
    restartStream: () => ipcRenderer.invoke('stream:restart'),
    getNVRConfig: () => ipcRenderer.invoke('nvr:get-config'),
    saveNVRConfig: (sources) => ipcRenderer.invoke('nvr:save-config', sources),
    checkUpdate: () => ipcRenderer.invoke('update:check'),
    installUpdate: (update) => ipcRenderer.invoke('update:install', update),
    onStreamStatus: (callback) => {
        const listener = (_event, value) => callback(value)
        ipcRenderer.on('stream:status-changed', listener)
        return () => ipcRenderer.off('stream:status-changed', listener)
    },
    onNavigate: (callback) => {
        const listener = (_event, page) => callback(page)
        ipcRenderer.on('app:navigate', listener)
        return () => ipcRenderer.off('app:navigate', listener)
    }
})
