import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('eyesAPI', {
    getStatus: () => ipcRenderer.invoke('app:get-status'),
    registerDevice: () => ipcRenderer.invoke('device:register'),
    restartStream: () => ipcRenderer.invoke('stream:restart'),
    onStreamStatus: (callback) => {
        const listener = (_event, value) => callback(value)
        ipcRenderer.on('stream:status-changed', listener)
        return () => ipcRenderer.off('stream:status-changed', listener)
    },
    onRegistration: (callback) => {
        const listener = (_event, value) => callback(value)
        ipcRenderer.on('device:registration-changed', listener)
        return () => ipcRenderer.off('device:registration-changed', listener)
    }
})
