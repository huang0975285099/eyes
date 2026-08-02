import os from 'os'
import dgram from 'dgram'

const VIRTUAL = /virtual|vmware|hyper-v|vethernet|loopback|tunnel|docker|wsl|bluetooth/i

function targetAddress(value) {
    try {
        const url = new URL(value.includes('://') ? value : `http://${value}`)
        return { host: url.hostname, port: Number(url.port) || 80 }
    } catch {
        return { host: '8.8.8.8', port: 53 }
    }
}

function allNICs() {
    return Object.entries(os.networkInterfaces()).flatMap(([name, entries]) =>
        (entries || [])
            .filter(
                (entry) =>
                    entry.family === 'IPv4' && !entry.internal && entry.mac !== '00:00:00:00:00:00'
            )
            .map((entry) => ({ name, ip: entry.address, mac: entry.mac.toLowerCase() }))
    )
}

export async function getPreferredNICAsync(target = '') {
    const candidates = allNICs()
    if (!candidates.length) return { name: '', ip: '', mac: '' }
    const destination = targetAddress(target)
    const routedIP = await new Promise((resolve) => {
        const socket = dgram.createSocket('udp4')
        const done = (ip = '') => {
            try {
                socket.close()
            } catch (error) {
                console.debug('[network] UDP socket 已关闭:', error.message)
            }
            resolve(ip)
        }
        socket.once('error', () => done())
        socket.connect(destination.port, destination.host, () => done(socket.address().address))
        setTimeout(() => done(), 1500)
    })
    return (
        candidates.find((item) => item.ip === routedIP) ||
        candidates.find((item) => !VIRTUAL.test(item.name)) ||
        candidates[0]
    )
}

export function listNICs() {
    return allNICs()
}
