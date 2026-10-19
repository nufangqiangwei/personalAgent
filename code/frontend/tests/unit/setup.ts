import { config } from '@vue/test-utils'
import { afterEach, vi } from 'vitest'

// Default unit tests must never reach a real provider or the network.
afterEach(() => {
  vi.restoreAllMocks()
})

config.global.stubs = {}
