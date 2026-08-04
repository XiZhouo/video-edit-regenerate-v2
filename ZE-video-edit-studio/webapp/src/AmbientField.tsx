/**
 * AmbientField — 编辑器外壳「流动主题光」白底背景(opt-in,默认不挂载)。
 *
 * 一块带 alpha 的全幅 WebGL2 canvas,垫在所有玻璃面板之后(.ze-ambient-field,z-index:0)、
 * 压在 body 真白底(--ze-surface-canvas)之上:跑原版 Grainient 的整场运动(noise 旋转 +
 * 正弦 warp),颜色只在流动场强高的少数区域显形(其余 alpha→0 露真白),大片留白;色相 = 当前
 * 工具主题色(从 --ze-accent-500 解析色相 H,配两个邻近相),随明暗 / 主题切换实时重读。
 *
 * 由 02-mod/04-layout.config.ts 的 `ambientBackground='field'` 决定是否挂载(App.tsx 网关)。
 * 三道降级即不渲(露 body 静态渐变):无 WebGL2 / reduced-motion / 本组件根本未挂载。
 *
 * 颜色不写死:从 getComputedStyle 读 --ze-accent-500 取色相,经 1×1 canvas2d 让浏览器把 oklch 解
 * 成 sRGB 喂 uniform(shader gamma 不重编码 → 喂 sRGB 即与 CSS 一致)。全程无 #hex、无 color:rgb()
 * 内联样式(契约门禁红线);定位/尺寸全交给 .ze-ambient-field(L5 app-shell,纯结构 token-free)。
 */
import { useEffect, useRef } from 'react'

const VERT = `#version 300 es
in vec2 position;
void main(){ gl_Position = vec4(position, 0.0, 1.0); }`

const FRAG = `#version 300 es
precision highp float;
uniform vec2 iResolution; uniform float iTime;
uniform float uTimeSpeed,uWarpStrength,uWarpFrequency,uWarpSpeed,uWarpAmplitude,uRotationAmount,uNoiseScale,uZoom,uGrain,uWhiteCut,uPeak;
uniform vec3 uColor1,uColor2,uColor3;
out vec4 fragColor;
mat2 Rot(float a){float s=sin(a),c=cos(a);return mat2(c,-s,s,c);}
vec2 hash(vec2 p){p=vec2(dot(p,vec2(2127.1,81.17)),dot(p,vec2(1269.5,283.37)));return fract(sin(p)*43758.5453);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p),u=f*f*(3.0-2.0*f);float n=mix(mix(dot(-1.0+2.0*hash(i+vec2(0.0,0.0)),f-vec2(0.0,0.0)),dot(-1.0+2.0*hash(i+vec2(1.0,0.0)),f-vec2(1.0,0.0)),u.x),mix(dot(-1.0+2.0*hash(i+vec2(0.0,1.0)),f-vec2(0.0,1.0)),dot(-1.0+2.0*hash(i+vec2(1.0,1.0)),f-vec2(1.0,1.0)),u.x),u.y);return 0.5+0.5*n;}
void main(){
  vec2 C=gl_FragCoord.xy; float t=iTime*uTimeSpeed;
  vec2 uv=C/iResolution.xy; float ratio=iResolution.x/iResolution.y;
  vec2 tuv=uv-0.5; tuv/=max(uZoom,0.001);
  float degree=noise(vec2(t*0.1,tuv.x*tuv.y)*uNoiseScale);
  tuv.y*=1.0/ratio; tuv*=Rot(radians((degree-0.5)*uRotationAmount+180.0)); tuv.y*=ratio;
  float ws=max(uWarpStrength,0.001); float amp=uWarpAmplitude/ws; float wt=t*uWarpSpeed;
  tuv.x+=sin(tuv.y*uWarpFrequency+wt)/amp; tuv.y+=sin(tuv.x*(uWarpFrequency*1.5)+wt)/(amp*0.5);
  float band=0.5+0.5*sin(tuv.x*1.4 + tuv.y*0.9);
  float nz=noise(tuv*1.2 + vec2(t*0.22,-t*0.16));
  float field=0.5*band + 0.5*nz;
  float a=smoothstep(uWhiteCut, uWhiteCut+0.36, field)*uPeak;
  float hueF=noise(tuv*0.9 + vec2(-t*0.15,t*0.2) + 11.0);
  vec3 color=mix(uColor2,uColor1,smoothstep(0.3,0.85,hueF));
  color=mix(color,uColor3,smoothstep(0.55,0.95,band)*0.4);
  float g=fract(sin(dot(uv*vec2(312.0,231.0),vec2(12.9898,78.233)))*43758.5453);
  color+=(g-0.5)*uGrain;
  fragColor=vec4(clamp(color,0.0,1.0), clamp(a,0.0,1.0));
}`

// 鲜艳度(彩度倍率,定稿默认);流动/扭曲/白占比常量(原版运动 + v5 阈值留白)。
const K = 0.5
const MOTION: Record<string, number> = {
  uTimeSpeed: 0.22, uWarpStrength: 1.0, uWarpFrequency: 5.0, uWarpSpeed: 2.0,
  uWarpAmplitude: 50.0, uRotationAmount: 500.0, uNoiseScale: 2.0, uZoom: 0.9,
  uGrain: 0.04, uWhiteCut: 0.52, uPeak: 0.72,
}

/** 从 oklch token 串解析色相(第 3 个数);缺失/异常回退 250。 */
function hueOf(oklch: string): number {
  const nums = oklch.match(/[\d.]+/g)
  const h = nums && nums.length >= 3 ? parseFloat(nums[2]) : NaN
  return Number.isFinite(h) ? h : 250
}

/** 三档光色 oklch 串:主题相 H + 亮邻近 H+50 + 灰邻近 H+22;明暗各一组,彩度乘 K。 */
function lightStops(h: number): [string, string, string] {
  const c1 = (0.13 * K).toFixed(3), c2 = (0.17 * K).toFixed(3), c3 = (0.09 * K).toFixed(3)
  return [`oklch(80% ${c1} ${h + 50})`, `oklch(66% ${c2} ${h})`, `oklch(74% ${c3} ${h + 22})`]
}
function darkStops(h: number): [string, string, string] {
  const c1 = (0.15 * K).toFixed(3), c2 = (0.17 * K).toFixed(3), c3 = (0.10 * K).toFixed(3)
  return [`oklch(70% ${c1} ${h + 50})`, `oklch(58% ${c2} ${h})`, `oklch(52% ${c3} ${h + 22})`]
}

export function AmbientField() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: false, antialias: true })
    if (!gl) return // 无 WebGL2 → 不渲,body 白底/静态渐变透出

    const compile = (type: number, src: string) => {
      const sh = gl.createShader(type)!
      gl.shaderSource(sh, src)
      gl.compileShader(sh)
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.error('AmbientField shader:', gl.getShaderInfoLog(sh))
      }
      return sh
    }
    const program = gl.createProgram()!
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERT))
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAG))
    gl.linkProgram(program)
    gl.useProgram(program)

    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
    const loc = gl.getAttribLocation(program, 'position')
    gl.enableVertexAttribArray(loc)
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0)

    const U = (n: string) => gl.getUniformLocation(program, n)
    for (const [name, value] of Object.entries(MOTION)) gl.uniform1f(U(name), value)
    const uRes = U('iResolution'), uTime = U('iTime')
    const uC = [U('uColor1'), U('uColor2'), U('uColor3')]

    // oklch → sRGB:让浏览器解析 oklch(getComputedStyle 读出 / 由色相构造),取像素值。无 #hex、无 color:rgb()。
    const oc = document.createElement('canvas')
    oc.width = oc.height = 1
    const octx = oc.getContext('2d')!
    const toRgb = (s: string): [number, number, number] => {
      octx.clearRect(0, 0, 1, 1)
      octx.fillStyle = s
      octx.fillRect(0, 0, 1, 1)
      const d = octx.getImageData(0, 0, 1, 1).data
      return [d[0] / 255, d[1] / 255, d[2] / 255]
    }

    const readColors = () => {
      const root = document.documentElement
      const cs = getComputedStyle(root)
      const h = hueOf(cs.getPropertyValue('--ze-accent-500').trim())
      const dark = root.getAttribute('data-theme') === 'dark'
      const stops = (dark ? darkStops : lightStops)(h)
      gl.useProgram(program)
      stops.forEach((s, i) => {
        const [r, g, b] = toRgb(s)
        gl.uniform3f(uC[i], r, g, b)
      })
    }

    const setSize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const w = Math.max(1, Math.floor(canvas.clientWidth * dpr))
      const h = Math.max(1, Math.floor(canvas.clientHeight * dpr))
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w
        canvas.height = h
      }
      gl.viewport(0, 0, canvas.width, canvas.height)
    }

    const ro = new ResizeObserver(setSize)
    ro.observe(canvas)
    setSize()
    readColors()

    const t0 = performance.now()
    let raf = 0
    let isVisible = true
    let isPageVisible = !document.hidden
    const reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)')

    const loop = (t: number) => {
      gl.clearColor(0, 0, 0, 0)
      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.uniform2f(uRes, canvas.width, canvas.height)
      gl.uniform1f(uTime, (t - t0) * 0.001)
      gl.drawArrays(gl.TRIANGLES, 0, 3)
      raf = requestAnimationFrame(loop)
    }
    const start = () => {
      if (isVisible && isPageVisible && !reduceMq.matches && raf === 0) raf = requestAnimationFrame(loop)
    }
    const stop = () => {
      if (raf !== 0) { cancelAnimationFrame(raf); raf = 0 }
    }

    const io = new IntersectionObserver(([e]) => {
      isVisible = e.isIntersecting
      isVisible ? start() : stop()
    }, { threshold: 0 })
    io.observe(canvas)

    const onVisibility = () => {
      isPageVisible = !document.hidden
      isPageVisible ? start() : stop()
    }
    document.addEventListener('visibilitychange', onVisibility)

    const onReduce = () => { reduceMq.matches ? stop() : start() }
    reduceMq.addEventListener('change', onReduce)

    // 主题相 / 明暗切换 → 重读光色(不重建上下文)
    const mo = new MutationObserver(readColors)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-ze-theme'] })

    start()

    return () => {
      stop()
      ro.disconnect()
      io.disconnect()
      mo.disconnect()
      document.removeEventListener('visibilitychange', onVisibility)
      reduceMq.removeEventListener('change', onReduce)
    }
  }, [])

  return <canvas ref={canvasRef} className="ze-ambient-field" aria-hidden="true" />
}
