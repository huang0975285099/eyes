import { spawnSync } from 'node:child_process'

const result = spawnSync('npx', ['quasar', 'build'], {
  stdio: 'inherit',
  shell: process.platform === 'win32',
  env: { ...process.env, NATIVE_BUILD: '1' },
})
process.exit(result.status ?? 1)
