/**
 * The dreamy point cloud behind every screen, ported from the design
 * prototype's `particle-cloud.js` (Claude Design: Meditation PWA Prototype).
 * The rendering logic — brightness→depth shader, breathing erosion, wind
 * dispersion, the mood tiers and focus poses — is kept verbatim; only the
 * custom-element plumbing became React props.
 *
 * Props map 1:1 to the prototype's attributes:
 *   paused    stop advancing (the canvas keeps its last frame)
 *   focus     'lines' | 'frame' squash the cloud while a home button is held
 *   mood      hero > ambient > whisper > settle — density/brightness/drift tiers
 *   pulse     slow amplitude swell while the player is playing
 *   calm      near-freeze drift while the player is paused
 *   dissolve  0..1, how far a sampled picture has scattered into stardust
 *   src       a user picture to sample instead of the procedural nebula
 *
 * In test environments (jsdom) there is no 2D canvas or WebGL; boot() bails
 * out silently and the component renders an empty layer.
 */
import { useEffect, useRef } from 'react'
import * as THREE from 'three'

export type CloudFocus = 'idle' | 'lines' | 'frame'
export type CloudMood = 'hero' | 'ambient' | 'whisper' | 'settle'

export interface ParticleCloudProps {
  paused?: boolean
  focus?: CloudFocus
  mood?: CloudMood
  pulse?: boolean
  calm?: boolean
  dissolve?: number
  intensity?: number
  src?: string
}

const VERT = `
  uniform float uTime, uSize, uSpeed, uNoiseStrength, uNoiseScale, uZDepth, uExplosion;
  uniform float uWindStrength, uDispersion, uRatio;
  uniform vec2 uWindDirection;
  attribute vec3 color;
  varying vec3 vColor; varying float vNoise; varying vec3 vPos;
  vec3 permute(vec3 x) { return mod(((x*34.0)+1.0)*x, 289.0); }
  float snoise(vec2 v){
    const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
    vec2 i = floor(v + dot(v, C.yy)); vec2 x0 = v - i + dot(i, C.xx);
    vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    vec4 x12 = x0.xyxy + C.xxzz; x12.xy -= i1; i = mod(i, 289.0);
    vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
    vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0);
    m = m*m; m = m*m;
    vec3 x = 2.0 * fract(p * C.www) - 1.0; vec3 h = abs(x) - 0.5;
    vec3 ox = floor(x + 0.5); vec3 a0 = x - ox;
    m *= 1.79284291400159 - 0.85373472095314 * (a0*a0 + h*h);
    vec3 g; g.x = a0.x * x0.x + h.x * x0.y; g.yz = a0.yz * x12.xz + h.yz * x12.yw;
    return 130.0 * dot(m, g);
  }
  void main() {
    vColor = color; vec3 pos = position; vPos = pos;
    float brightness = (color.r + color.g + color.b) / 3.0;
    pos.z += brightness * uZDepth;
    float maxRadius = (uRatio > 1.0) ? (5.0 / uRatio) : 5.0;
    float edgeInfluence = pow(length(pos.xy) / maxRadius, 3.0);
    float dispNoise = snoise(pos.xy * 2.0 + uTime * 0.1);
    vec2 radialDir = normalize(pos.xy + vec2(dispNoise, -dispNoise));
    float dispForce = uDispersion * edgeInfluence * 4.0;
    pos.xy += radialDir * dispForce;
    float timeFlow = uTime * uSpeed;
    float noise = snoise(pos.xy * uNoiseScale + timeFlow);
    vNoise = noise;
    pos += noise * uNoiseStrength * 0.2;
    vec2 windDir = normalize(uWindDirection + vec2(0.001));
    float windTurbulence = snoise(pos.xy * 1.5 - windDir * timeFlow * 2.0);
    float windForce = uWindStrength * (0.1 + edgeInfluence * 2.5);
    pos.xy += windDir * windForce * (1.0 + windTurbulence * 0.5);
    pos.z += windForce * windTurbulence * 1.0;
    pos += normalize(pos) * uExplosion * 10.0;
    vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
    float sizeFade = 1.0 / (1.0 + (dispForce + windForce) * 2.0);
    gl_PointSize = uSize * (1.0 + noise * 0.3) * sizeFade * (200.0 / -mvPosition.z);
    gl_Position = projectionMatrix * mvPosition;
  }`

const FRAG = `
  uniform float uOpacity, uContrast, uThreshold, uEdgeSoftness, uEdgeNoiseStrength;
  uniform float uErosionAmount, uErosionNoise, uRatio;
  varying vec3 vColor; varying float vNoise; varying vec3 vPos;
  void main() {
    vec2 coord = gl_PointCoord - vec2(0.5);
    float dist = length(coord);
    if (dist > 0.5) discard;
    float glow = pow(1.0 - dist * 2.0, 1.5);
    float luminance = dot(vColor, vec3(0.299, 0.587, 0.114));
    float effectiveThreshold = uThreshold + vNoise * uEdgeNoiseStrength;
    float alphaFade = smoothstep(effectiveThreshold, effectiveThreshold + uEdgeSoftness, luminance);
    float maxRadius = (uRatio > 1.0) ? (5.0 / uRatio) : 5.0;
    float jaggedDist = length(vPos.xy) + vNoise * uErosionNoise * 0.5;
    float normalizedDist = jaggedDist / maxRadius;
    float limit = 1.0 - uErosionAmount;
    float spatialMask = 1.0 - smoothstep(limit - 0.05, limit + 0.05, normalizedDist);
    if (alphaFade < 0.01 || spatialMask < 0.01) discard;
    vec3 finalColor = (vColor - 0.5) * max(uContrast, 0.0) + 0.5;
    gl_FragColor = vec4(finalColor, uOpacity * alphaFade * spatialMask * glow);
  }`

interface CloudData {
  positions: Float32Array
  colors: Float32Array
}

/** Default source image: soft luminous nebula, drawn procedurally. */
function sourceCanvas(size: number): ImageData | null {
  const c = document.createElement('canvas')
  c.width = c.height = size
  const x = c.getContext('2d')
  if (!x) return null
  x.fillStyle = '#05060f'
  x.fillRect(0, 0, size, size)
  const blob = (cx: number, cy: number, r: number, col: string, a: number) => {
    const g = x.createRadialGradient(cx * size, cy * size, 0, cx * size, cy * size, r * size)
    g.addColorStop(0, col.replace('ALPHA', String(a)))
    g.addColorStop(0.45, col.replace('ALPHA', String(a * 0.42)))
    g.addColorStop(1, col.replace('ALPHA', '0'))
    x.fillStyle = g
    x.beginPath()
    x.arc(cx * size, cy * size, r * size, 0, Math.PI * 2)
    x.fill()
  }
  x.globalCompositeOperation = 'lighter'
  blob(0.5, 0.48, 0.33, 'rgba(96,110,240,ALPHA)', 0.55)
  blob(0.41, 0.39, 0.17, 'rgba(186,164,255,ALPHA)', 0.85)
  blob(0.63, 0.58, 0.15, 'rgba(255,206,150,ALPHA)', 0.55)
  blob(0.33, 0.63, 0.14, 'rgba(96,158,255,ALPHA)', 0.5)
  blob(0.67, 0.35, 0.12, 'rgba(214,150,255,ALPHA)', 0.4)
  blob(0.5, 0.5, 0.46, 'rgba(52,64,170,ALPHA)', 0.22)
  // wisps
  for (let i = 0; i < 14; i++) {
    const a0 = Math.random() * Math.PI * 2
    const r0 = 0.16 + Math.random() * 0.26
    x.strokeStyle = 'rgba(190,195,255,' + (0.05 + Math.random() * 0.09).toFixed(3) + ')'
    x.lineWidth = size * (0.006 + Math.random() * 0.02)
    x.beginPath()
    for (let t = 0; t <= 1.001; t += 0.05) {
      const a = a0 + t * 1.5
      const r = r0 * (1 + t * 0.5)
      const px = (0.5 + Math.cos(a) * r) * size
      const py = (0.5 + Math.sin(a) * r * 0.8) * size
      if (t === 0) x.moveTo(px, py)
      else x.lineTo(px, py)
    }
    x.stroke()
  }
  return x.getImageData(0, 0, size, size)
}

function pixelData(img: ImageData, step: number, satOverride?: number): CloudData {
  const pos: number[] = []
  const col: number[] = []
  const W = img.width
  const H = img.height
  for (let j = 0; j < H; j += step) {
    for (let i = 0; i < W; i += step) {
      const k = (j * W + i) * 4
      const r = img.data[k] / 255
      const g = img.data[k + 1] / 255
      const b = img.data[k + 2] / 255
      if ((r + g + b) / 3 < 0.055) continue
      const lum = 0.299 * r + 0.587 * g + 0.114 * b
      const sat = satOverride || 1.75
      const nx = i / (W - 1) - 0.5
      const ny = j / (H - 1) - 0.5
      if (Math.hypot(nx, ny) > 0.5) continue
      const jit = step * 0.045
      pos.push(
        nx * 10 + (Math.random() - 0.5) * jit,
        -ny * 10 + (Math.random() - 0.5) * jit,
        (Math.random() - 0.5) * 1.4,
      )
      col.push(
        Math.min(1, lum + (r - lum) * sat),
        Math.min(1, lum + (g - lum) * sat),
        Math.min(1, lum + (b - lum) * sat),
      )
    }
  }
  return { positions: new Float32Array(pos), colors: new Float32Array(col) }
}

// density / brightness / drift tiers — hero > performer > atmosphere > whisper
const MOODS: Record<CloudMood, { thr: number; op: number; size: number; drift: number }> = {
  hero: { thr: 0.085, op: 1.0, size: 1.0, drift: 1.0 },
  ambient: { thr: 0.2, op: 0.85, size: 0.8, drift: 0.5 },
  whisper: { thr: 0.33, op: 0.34, size: 0.6, drift: 0.28 },
  settle: { thr: 0.32, op: 0.36, size: 0.62, drift: 0.2 },
}

export default function ParticleCloud(props: ParticleCloudProps) {
  const hostRef = useRef<HTMLDivElement>(null)

  // The RAF loop reads live prop values through this ref, exactly the way the
  // custom element re-read its attributes every frame.
  const propsRef = useRef(props)
  useEffect(() => {
    propsRef.current = props
  })

  // Set by boot(); lets the src effect swap geometry without a re-boot.
  const applyRef = useRef<((d: CloudData) => void) | null>(null)
  const defaultDataRef = useRef<CloudData | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    let raf = 0
    let renderer: THREE.WebGLRenderer | null = null
    let ro: ResizeObserver | null = null
    let geo: THREE.BufferGeometry | null = null
    let mat: THREE.ShaderMaterial | null = null

    try {
      const source = sourceCanvas(260)
      if (!source) return
      const scene = new THREE.Scene()
      const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 200)
      camera.position.set(0, 0, 12.2)
      renderer = new THREE.WebGLRenderer({
        alpha: true,
        antialias: false,
        preserveDrawingBuffer: true,
      })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
      renderer.setClearColor(0x000000, 0)
      host.appendChild(renderer.domElement)
      renderer.domElement.style.display = 'block'
      renderer.domElement.style.width = '100%'
      renderer.domElement.style.height = '100%'

      const build = (d: CloudData) => {
        const g = new THREE.BufferGeometry()
        g.setAttribute('position', new THREE.BufferAttribute(d.positions, 3))
        g.setAttribute('color', new THREE.BufferAttribute(d.colors, 3))
        g.center()
        return g
      }
      defaultDataRef.current = pixelData(source, 3)
      geo = build(defaultDataRef.current)

      const uniforms = {
        uTime: { value: 0 },
        uSize: { value: 2.5 },
        uOpacity: { value: 0.66 },
        uSpeed: { value: 0.06 },
        uNoiseStrength: { value: 0.5 },
        uNoiseScale: { value: 0.22 },
        uContrast: { value: 1.25 },
        uZDepth: { value: 1.0 },
        uExplosion: { value: 0 },
        uThreshold: { value: 0.085 },
        uEdgeSoftness: { value: 0.22 },
        uEdgeNoiseStrength: { value: 0.05 },
        uErosionAmount: { value: 0.04 },
        uErosionNoise: { value: 0.3 },
        uRatio: { value: 1 },
        uWindStrength: { value: 0.06 },
        uWindDirection: { value: new THREE.Vector2(0.6, 0.25) },
        uDispersion: { value: 0.12 },
      }
      mat = new THREE.ShaderMaterial({
        vertexShader: VERT,
        fragmentShader: FRAG,
        uniforms,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
      const points = new THREE.Points(geo, mat)
      applyRef.current = (d) => {
        const old = points.geometry
        points.geometry = build(d)
        old.dispose()
      }
      points.rotation.x = -0.06
      points.position.y = 0.5
      scene.add(points)

      const resize = () => {
        const w = host.clientWidth || 390
        const h = host.clientHeight || 700
        renderer!.setSize(w, h, false)
        renderer!.domElement.style.width = '100%'
        renderer!.domElement.style.height = '100%'
        camera.aspect = w / h
        camera.updateProjectionMatrix()
      }
      ro = new ResizeObserver(resize)
      ro.observe(host)
      resize()

      const lerp = (a: number, b: number, t: number) => a + (b - a) * t
      let sy = 1
      let sx = 1
      let wind = 1
      let clock = 0
      let last = performance.now()
      let amp = 0.5
      let settleY = 0
      const rmq = window.matchMedia('(prefers-reduced-motion: reduce)')
      const loop = () => {
        raf = requestAnimationFrame(loop)
        const p = propsRef.current
        if (p.paused) return
        const nowMs = performance.now()
        const dt = Math.min(0.05, (nowMs - last) / 1000)
        last = nowMs
        const M = MOODS[p.mood ?? 'hero']
        const ambient = (p.mood ?? 'hero') !== 'hero'
        const still = rmq.matches
        clock += still ? 0 : dt * (p.calm ? 0.08 : 1) * M.drift
        const time = clock
        if (p.pulse) {
          const target = 0.5 + 0.5 * Math.sin(clock * 1.15) * Math.sin(clock * 0.37 + 1.1)
          amp += (target - amp) * 0.04
        } else amp = 0.5
        if (p.mood === 'settle' && !still) settleY = (settleY + dt * 0.055) % 1
        if (still && clock === 0) clock = 12.5 // one static scattered arrangement
        const k = p.intensity ?? 1
        const dis = Math.max(0, Math.min(1, p.dissolve ?? 1))
        const focus = p.focus ?? 'idle'
        uniforms.uTime.value = time
        const phase = (Math.sin(time * 0.15) + 1) * 0.5 // ~40s breath
        uniforms.uErosionAmount.value = lerp(0.03, 0.11, phase)
        uniforms.uErosionNoise.value = lerp(0.5, 1.15, phase)
        uniforms.uZDepth.value = lerp(0.7, 1.9, phase)
        uniforms.uExplosion.value = lerp(0, 0.02, phase)
        uniforms.uDispersion.value = lerp(0.08, 0.2, phase) * k * dis
        uniforms.uWindStrength.value = 0.05 * k * wind * dis * M.drift * (still ? 0 : 1)
        uniforms.uNoiseStrength.value = 0.5 * dis
        uniforms.uSize.value = lerp(1.25, 2.5, dis) * M.size * (1 + (amp - 0.5) * 0.1)
        uniforms.uOpacity.value = lerp(0.95, 0.66, dis) * M.op * (1 + (amp - 0.5) * 0.08)
        uniforms.uThreshold.value = M.thr
        uniforms.uErosionAmount.value *= dis
        uniforms.uZDepth.value *= 0.25 + 0.75 * dis
        const tSy = focus === 'lines' ? 0.34 : focus === 'frame' ? 0.78 : 1
        const tSx = focus === 'frame' ? 0.82 : 1
        const tWind = focus === 'lines' ? 4.2 : focus === 'frame' ? 0.25 : 1
        sy = lerp(sy, tSy, 0.05)
        sx = lerp(sx, tSx, 0.05)
        wind = lerp(wind, tWind, 0.05)
        points.scale.set(sx, sy, 1)
        points.rotation.y =
          Math.sin(time * (ambient ? 0.018 : 0.035)) * 0.34 * (focus === 'idle' ? 1 : 0.3)
        points.rotation.z = Math.sin(time * 0.021) * 0.06
        points.position.y = 0.5 - settleY * 1.6
        renderer!.render(scene, camera)
      }
      loop()
    } catch {
      // No WebGL (jsdom, ancient browser): the background simply stays flat.
      return
    }

    return () => {
      cancelAnimationFrame(raf)
      ro?.disconnect()
      applyRef.current = null
      geo?.dispose()
      mat?.dispose()
      if (renderer) {
        renderer.dispose()
        renderer.domElement.remove()
      }
    }
  }, [])

  // Sample a user picture (cover-cropped to a square) and swap the geometry in
  // place — or fall back to the procedural nebula when src clears.
  useEffect(() => {
    const apply = applyRef.current
    if (!apply) return
    if (!props.src) {
      if (defaultDataRef.current) apply(defaultDataRef.current)
      return
    }
    let stale = false
    const img = new Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => {
      if (stale) return
      const S = 190
      const c = document.createElement('canvas')
      c.width = c.height = S
      const x = c.getContext('2d')
      if (!x) return
      const side = Math.min(img.width, img.height)
      x.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, S, S)
      applyRef.current?.(pixelData(x.getImageData(0, 0, S, S), 1, 1.15))
    }
    img.src = props.src
    return () => {
      stale = true
    }
  }, [props.src])

  return (
    <div
      ref={hostRef}
      aria-hidden
      style={{ position: 'absolute', inset: 0, overflow: 'hidden', display: 'block' }}
    />
  )
}
