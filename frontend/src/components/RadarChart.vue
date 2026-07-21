<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    axes: string[]
    /** 每条序列: 名称 + 颜色 + 与 axes 等长的值(0-max) */
    series: { name: string; color: string; values: number[] }[]
    max?: number
    size?: number
  }>(),
  { max: 10, size: 320 },
)

const cx = computed(() => props.size / 2)
const cy = computed(() => props.size / 2)
const R = computed(() => props.size / 2 - 42)

function pointAt(i: number, ratio: number): { x: number; y: number } {
  const n = props.axes.length
  const ang = -Math.PI / 2 + (i * 2 * Math.PI) / n
  const r = ratio * R.value
  return { x: cx.value + r * Math.cos(ang), y: cy.value + r * Math.sin(ang) }
}

// 同心网格(4 环)
const rings = computed(() => {
  const steps = [0.25, 0.5, 0.75, 1]
  return steps.map((s) =>
    props.axes.map((_, i) => pointAt(i, s)).map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '),
  )
})

const spokes = computed(() =>
  props.axes.map((_, i) => {
    const p = pointAt(i, 1)
    return { x2: p.x, y2: p.y }
  }),
)

const axisLabels = computed(() =>
  props.axes.map((label, i) => {
    const p = pointAt(i, 1.16)
    return { label, x: p.x, y: p.y }
  }),
)

function seriesPoints(values: number[]): string {
  return values
    .map((v, i) => {
      const ratio = Math.max(0, Math.min(1, v / props.max))
      const p = pointAt(i, ratio)
      return `${p.x.toFixed(1)},${p.y.toFixed(1)}`
    })
    .join(' ')
}
</script>

<template>
  <div class="radar">
    <svg :width="size" :height="size" :viewBox="`0 0 ${size} ${size}`">
      <!-- 网格环 -->
      <polygon
        v-for="(ring, ri) in rings"
        :key="'ring' + ri"
        :points="ring"
        fill="none"
        stroke="#e2e8f0"
        stroke-width="1"
      />
      <!-- 轴线 -->
      <line
        v-for="(sp, si) in spokes"
        :key="'spoke' + si"
        :x1="cx" :y1="cy" :x2="sp.x2" :y2="sp.y2"
        stroke="#e2e8f0" stroke-width="1"
      />
      <!-- 各序列多边形 -->
      <g v-for="(s, i) in series" :key="'s' + i">
        <polygon
          :points="seriesPoints(s.values)"
          :fill="s.color" fill-opacity="0.12"
          :stroke="s.color" stroke-width="2"
        />
        <circle
          v-for="(v, vi) in s.values"
          :key="'pt' + i + '-' + vi"
          :cx="pointAt(vi, Math.max(0, Math.min(1, v / max))).x"
          :cy="pointAt(vi, Math.max(0, Math.min(1, v / max))).y"
          r="2.5" :fill="s.color"
        />
      </g>
      <!-- 轴标签 -->
      <text
        v-for="(a, ai) in axisLabels"
        :key="'ax' + ai"
        :x="a.x" :y="a.y"
        text-anchor="middle" dominant-baseline="middle"
        font-size="12" fill="#475569"
      >{{ a.label }}</text>
    </svg>
    <!-- 图例 -->
    <div class="legend">
      <span v-for="(s, i) in series" :key="'lg' + i" class="lg">
        <i class="dot" :style="{ background: s.color }"></i>{{ s.name }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.radar { display: inline-block; }
.legend { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 4px; }
.lg { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #475569; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
</style>
