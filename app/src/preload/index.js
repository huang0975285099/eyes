import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('eyesAPI', {
    getStatus: () => ipcRenderer.invoke('app:get-status'),
    setUserName: (value) => ipcRenderer.invoke('device:set-user-name', value),
    restartStream: () => ipcRenderer.invoke('stream:restart'),
    checkUpdate: () => ipcRenderer.invoke('update:check'),
    installUpdate: (update) => ipcRenderer.invoke('update:install', update),
    onStreamStatus: (callback) => {
        const listener = (_event, value) => callback(value)
        ipcRenderer.on('stream:status-changed', listener)
        return () => ipcRenderer.off('stream:status-changed', listener)
    }
})
