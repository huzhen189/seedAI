// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  createStreamUiState,
  reduceStreamEvent,
  type StreamUiState,
} from './reducer'

function ev(partial: Partial<{
  stream_id: string
  turn_id: string
  trace_id: string
  event_id: string
  seq: number
  type: string
  data: Record<string, unknown>
}>) {
  return {
    stream_id: 's1',
    turn_id: 't1',
    trace_id: 'r1',
    event_id: partial.event_id ?? `e${partial.seq ?? 0}`,
    seq: partial.seq ?? 1,
    type: partial.type ?? 'token',
    data: partial.data ?? {},
  } as any
}

describe('reducer (M9d) 乱序快照对账', () => {
  it('preserves one-time decision_nonce across pending_second approval update', () => {
    const state: StreamUiState = createStreamUiState()
    reduceStreamEvent(state, ev({ seq: 1, type: 'approval', event_id: 'a1', data: {
      approval_id: 'ap1', status: 'pending_first', risk_level: 'critical', step: 2,
      decision_nonce: 'nonce-first', expires_at: null,
    } }))
    expect(state.approval?.decision_nonce).toBe('nonce-first')
    expect(state.approval?.status).toBe('pending_first')

    // 后端第一 approve 后下发的 pending_second 事件不带明文 nonce。
    reduceStreamEvent(state, ev({ seq: 2, type: 'approval', event_id: 'a2', data: {
      approval_id: 'ap1', status: 'pending_second', risk_level: 'critical', step: 2, expires_at: null,
    } }))
    expect(state.approval?.status).toBe('pending_second')
    // 关键: 一次性 nonce 必须保留, 否则第二段确认无法提交。
    expect(state.approval?.decision_nonce).toBe('nonce-first')
  })

  it('buffers out-of-order events and applies them in seq order', () => {
    const state: StreamUiState = createStreamUiState()
    // 以 seq=1 建立基线(实时流总是从首事件开始)。
    reduceStreamEvent(state, ev({ seq: 1, type: 'token', event_id: 't1', data: { delta: 'A' } }))
    expect(state.response).toBe('A')

    // 先到 seq=3(产生缺口): 不应应用, 且 gapAfter 指回最后连续位置。
    const r3 = reduceStreamEvent(state, ev({ seq: 3, type: 'token', event_id: 't3', data: { delta: 'C' } }))
    expect(r3.applied).toBe(false)
    expect(r3.gapAfter).toBe(1)
    expect(state.response).toBe('A')

    // 补到 seq=2 -> 按 2 再 3 顺序应用。
    const r2 = reduceStreamEvent(state, ev({ seq: 2, type: 'token', event_id: 't2', data: { delta: 'B' } }))
    expect(r2.applied).toBe(true)
    expect(state.response).toBe('ABC') // B 在 C 前
    expect(state.lastSeq).toBe(3)
  })

  it('applies state_diff only when version is newer (CAS)', () => {
    const state: StreamUiState = createStreamUiState()
    reduceStreamEvent(state, ev({ seq: 1, type: 'state_diff', event_id: 'd1', data: { version: 3, patch: { k: 'v3' } } }))
    expect(state.state.k).toBe('v3')
    expect(state.stateVersion).toBe(3)

    // 迟到/乱序的旧版本快照必须被忽略, 不回退状态。
    reduceStreamEvent(state, ev({ seq: 2, type: 'state_diff', event_id: 'd2', data: { version: 1, patch: { k: 'v1' } } }))
    expect(state.state.k).toBe('v3')
    expect(state.stateVersion).toBe(3)
  })

  it('dedups by (stream_id, event_id)', () => {
    const state: StreamUiState = createStreamUiState()
    const first = reduceStreamEvent(state, ev({ seq: 1, type: 'token', event_id: 'dup', data: { delta: 'X' } }))
    const second = reduceStreamEvent(state, ev({ seq: 2, type: 'token', event_id: 'dup', data: { delta: 'Y' } }))
    expect(first.applied).toBe(true)
    expect(second.applied).toBe(false)
    expect(state.response).toBe('X')
  })
})
