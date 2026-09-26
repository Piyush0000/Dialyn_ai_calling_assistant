/* Dialyn avatar: a living, 3D-looking AI face drawn on a 2D canvas.
 *
 *   const avatar = new DialynAvatar(canvasElement, { size: "fill" });
 *   avatar.setState("idle" | "listening" | "thinking" | "speaking" | "happy" | "ended");
 *   avatar.setLevels(agentLevel, userLevel);   // 0..1, e.g. from audioLevel()
 *
 * No libraries, no WebGL: layered gradients, glow and a fake-3D particle orbit.
 * Respects prefers-reduced-motion.
 */
(function () {
  "use strict";

  const PALETTE = {
    idle: [139, 92, 246],      // violet
    listening: [34, 211, 238], // cyan
    thinking: [244, 114, 182], // pink
    speaking: [168, 85, 247],  // purple
    happy: [52, 211, 153],     // emerald
    ended: [100, 116, 139],    // slate
  };
  const ACCENT = {
    idle: [236, 72, 153],
    listening: [99, 102, 241],
    thinking: [251, 191, 36],
    speaking: [236, 72, 153],
    happy: [34, 211, 238],
    ended: [71, 85, 105],
  };

  const lerp = (a, b, t) => a + (b - a) * t;
  const mix = (c1, c2, t) => c1.map((v, i) => lerp(v, c2[i], t));
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

  class DialynAvatar {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "idle";
      this.color = PALETTE.idle.slice();
      this.accent = ACCENT.idle.slice();
      this.agent = 0;
      this.user = 0;
      this.agentTarget = 0;
      this.userTarget = 0;
      this.look = { x: 0, y: 0 };
      this.lookTarget = { x: 0, y: 0 };
      this.blink = 0;
      this.nextBlink = performance.now() + 1500;
      this.mouthPhase = 0;
      this.particles = Array.from({ length: opts.particles ?? 64 }, (_, i) => ({
        a: Math.random() * Math.PI * 2,
        r: 1.35 + Math.random() * 0.45,
        s: (0.15 + Math.random() * 0.35) * (i % 2 ? 1 : -1),
        size: 0.6 + Math.random() * 1.6,
        tilt: (Math.random() - 0.5) * 0.9,
      }));
      this.ring = new Float32Array(72);
      this.running = true;
      this._resize = this._resize.bind(this);
      this._frame = this._frame.bind(this);
      this._onPointer = (e) => {
        const r = this.canvas.getBoundingClientRect();
        const x = (e.clientX - (r.left + r.width / 2)) / (window.innerWidth / 2);
        const y = (e.clientY - (r.top + r.height / 2)) / (window.innerHeight / 2);
        this.lookTarget = { x: Math.max(-1, Math.min(1, x)), y: Math.max(-1, Math.min(1, y)) };
      };
      window.addEventListener("pointermove", this._onPointer, { passive: true });
      new ResizeObserver(this._resize).observe(canvas);
      this._resize();
      requestAnimationFrame(this._frame);
    }

    setState(state) {
      if (PALETTE[state]) this.state = state;
    }

    setLevels(agent, user) {
      this.agentTarget = Math.max(0, Math.min(1, agent || 0));
      this.userTarget = Math.max(0, Math.min(1, user || 0));
    }

    destroy() {
      this.running = false;
      window.removeEventListener("pointermove", this._onPointer);
    }

    _resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const { width, height } = this.canvas.getBoundingClientRect();
      this.w = width;
      this.h = height;
      this.canvas.width = Math.max(1, Math.round(width * dpr));
      this.canvas.height = Math.max(1, Math.round(height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _frame(now) {
      if (!this.running) return;
      const t = now / 1000;
      const speed = reduceMotion ? 0.25 : 1;
      this.color = mix(this.color, PALETTE[this.state], 0.06);
      this.accent = mix(this.accent, ACCENT[this.state], 0.06);
      this.agent = lerp(this.agent, this.agentTarget, this.agentTarget > this.agent ? 0.45 : 0.12);
      this.user = lerp(this.user, this.userTarget, this.userTarget > this.user ? 0.45 : 0.12);
      this.look.x = lerp(this.look.x, this.lookTarget.x, 0.06);
      this.look.y = lerp(this.look.y, this.lookTarget.y, 0.06);

      if (now > this.nextBlink) {
        this.blink = 1;
        this.nextBlink = now + 2200 + Math.random() * 3200;
      }
      this.blink = Math.max(0, this.blink - 0.12);
      this.mouthPhase += (0.25 + this.agent * 0.9) * speed;

      this._draw(t * speed);
      requestAnimationFrame(this._frame);
    }

    _draw(t) {
      const { ctx, w, h } = this;
      if (!w || !h) return;
      ctx.clearRect(0, 0, w, h);
      const size = Math.min(w, h);
      const R = size * 0.25;
      const cx = w / 2;
      const bob = Math.sin(t * 1.3) * R * 0.035;
      const cy = h / 2 + bob;
      const energy = Math.max(this.agent, this.user * 0.8);
      const c = this.color;
      const a = this.accent;
      const lx = this.look.x;
      const ly = this.look.y;

      // Aura
      // Fade out before the canvas edge so no hard rectangle ever shows.
      const auraR = Math.min(R * (2.3 + energy * 0.5), size / 2 - 2);
      const aura = ctx.createRadialGradient(cx, cy, R * 0.6, cx, h / 2, auraR);
      aura.addColorStop(0, rgba(c, 0.38 + energy * 0.25));
      aura.addColorStop(0.45, rgba(a, 0.12 + energy * 0.1));
      aura.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = aura;
      ctx.fillRect(0, 0, w, h);

      // Voice ring (bars around the head)
      const bars = this.ring.length;
      for (let i = 0; i < bars; i++) {
        const noise =
          Math.sin(t * 3 + i * 0.7) * 0.5 + Math.sin(t * 5.3 + i * 1.9) * 0.3 + Math.sin(t * 1.7 + i * 0.3) * 0.2;
        const target = 0.08 + Math.max(0, noise) * (0.1 + energy * 0.9);
        this.ring[i] = lerp(this.ring[i], target, 0.25);
      }
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * 0.15);
      ctx.lineCap = "round";
      for (let i = 0; i < bars; i++) {
        const ang = (i / bars) * Math.PI * 2;
        const r0 = R * 1.28;
        const r1 = r0 + R * Math.min(this.ring[i], 1) * 0.55;
        const k = i / bars;
        ctx.strokeStyle = rgba(mix(c, a, (Math.sin(k * Math.PI * 2 + t) + 1) / 2), 0.55 + energy * 0.4);
        ctx.lineWidth = Math.max(1.5, R * 0.028);
        ctx.beginPath();
        ctx.moveTo(Math.cos(ang) * r0, Math.sin(ang) * r0);
        ctx.lineTo(Math.cos(ang) * r1, Math.sin(ang) * r1);
        ctx.stroke();
      }
      ctx.restore();

      // Orbiting particles: back half first, sphere, then front half (fake depth)
      const orbit = (front) => {
        for (const p of this.particles) {
          const ang = p.a + t * p.s * (this.state === "thinking" ? 3 : 1);
          const z = Math.sin(ang);
          if (front !== z > 0) continue;
          const px = Math.cos(ang) * R * p.r;
          const py = Math.sin(ang) * R * p.r * 0.32 + Math.cos(ang) * R * p.tilt * 0.4;
          const depth = (z + 1) / 2;
          ctx.fillStyle = rgba(mix(c, [255, 255, 255], depth * 0.5), 0.25 + depth * 0.65);
          ctx.beginPath();
          ctx.arc(cx + px, cy + py, (p.size * (0.5 + depth)) * (size / 420), 0, Math.PI * 2);
          ctx.fill();
        }
      };
      orbit(false);

      // Head sphere
      ctx.save();
      ctx.shadowColor = rgba(c, 0.8);
      ctx.shadowBlur = R * (0.35 + energy * 0.4);
      const hx = cx - R * (0.35 - lx * 0.12);
      const hy = cy - R * (0.4 - ly * 0.12);
      const body = ctx.createRadialGradient(hx, hy, R * 0.05, cx, cy, R * 1.05);
      body.addColorStop(0, rgba(mix(c, [255, 255, 255], 0.75), 1));
      body.addColorStop(0.35, rgba(mix(c, [255, 255, 255], 0.15), 1));
      body.addColorStop(0.75, rgba(mix(c, [20, 10, 50], 0.45), 1));
      body.addColorStop(1, rgba([12, 8, 32], 1));
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      // Rim light
      const rim = ctx.createLinearGradient(cx - R, cy + R, cx + R, cy - R);
      rim.addColorStop(0, rgba(a, 0.9));
      rim.addColorStop(0.5, rgba(c, 0));
      rim.addColorStop(1, rgba([255, 255, 255], 0.35));
      ctx.strokeStyle = rim;
      ctx.lineWidth = R * 0.04;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.98, 0, Math.PI * 2);
      ctx.stroke();

      // Specular highlight
      const spec = ctx.createRadialGradient(hx, hy, 0, hx, hy, R * 0.45);
      spec.addColorStop(0, "rgba(255,255,255,0.55)");
      spec.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = spec;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();

      // Face (parallax toward the pointer)
      const fx = cx + lx * R * 0.12;
      const fy = cy + ly * R * 0.1;
      this._eyes(fx, fy, R, t);
      this._mouth(fx, fy, R, t);
      if (this.state === "happy" || this.state === "speaking") this._cheeks(fx, fy, R);
      if (this.state === "thinking") this._thinkingDots(cx, cy, R, t);

      orbit(true);
    }

    _eyes(fx, fy, R, t) {
      const { ctx } = this;
      const closed = this.state === "ended";
      const open = closed ? 0.12 : Math.max(0.08, 1 - this.blink * 0.95);
      const wide = this.state === "listening" ? 1.15 : this.state === "happy" ? 0.9 : 1;
      const ew = R * 0.13;
      const eh = R * 0.2 * open * wide;
      const px = this.look.x * R * 0.03;
      const py = this.look.y * R * 0.03;
      ctx.save();
      ctx.shadowColor = "rgba(255,255,255,0.9)";
      ctx.shadowBlur = R * 0.18;
      ctx.fillStyle = "rgba(255,255,255,0.96)";
      for (const side of [-1, 1]) {
        const x = fx + side * R * 0.33 + px;
        const y = fy - R * 0.1 + py;
        if (this.state === "happy") {
          // Smiling eyes: upward arcs
          ctx.strokeStyle = "rgba(255,255,255,0.96)";
          ctx.lineWidth = R * 0.07;
          ctx.lineCap = "round";
          ctx.beginPath();
          ctx.arc(x, y + R * 0.04, ew * 0.9, Math.PI * 1.1, Math.PI * 1.9);
          ctx.stroke();
          continue;
        }
        ctx.beginPath();
        ctx.ellipse(x, y, ew, Math.max(eh, R * 0.015), 0, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    _mouth(fx, fy, R, t) {
      const { ctx } = this;
      const mx = fx + this.look.x * R * 0.02;
      const my = fy + R * 0.32;
      ctx.save();
      ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(255,255,255,0.92)";
      ctx.fillStyle = "rgba(24,6,48,0.9)";
      ctx.shadowColor = "rgba(255,255,255,0.55)";
      ctx.shadowBlur = R * 0.07;
      ctx.lineWidth = R * 0.032;
      if (this.state === "speaking" || this.agent > 0.06) {
        const wobble = (Math.sin(this.mouthPhase) + 1) / 2;
        const open = R * (0.025 + this.agent * 0.13 + wobble * this.agent * 0.09);
        const width = R * (0.17 + this.agent * 0.05);
        ctx.beginPath();
        ctx.ellipse(mx, my, width, open, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      } else if (this.state === "listening") {
        ctx.beginPath();
        ctx.ellipse(mx, my, R * 0.07, R * 0.05 + this.user * R * 0.04, 0, 0, Math.PI * 2);
        ctx.stroke();
      } else if (this.state === "thinking") {
        const shift = Math.sin(t * 2) * R * 0.05;
        ctx.beginPath();
        ctx.moveTo(mx - R * 0.14 + shift, my);
        ctx.quadraticCurveTo(mx + shift, my + R * 0.04, mx + R * 0.14 + shift, my - R * 0.02);
        ctx.stroke();
      } else if (this.state === "ended") {
        ctx.beginPath();
        ctx.moveTo(mx - R * 0.12, my);
        ctx.lineTo(mx + R * 0.12, my);
        ctx.stroke();
      } else {
        const smile = this.state === "happy" ? 0.16 : 0.1;
        ctx.beginPath();
        ctx.moveTo(mx - R * 0.2, my - R * 0.03);
        ctx.quadraticCurveTo(mx, my + R * smile, mx + R * 0.2, my - R * 0.03);
        ctx.stroke();
      }
      ctx.restore();
    }

    _cheeks(fx, fy, R) {
      const { ctx } = this;
      for (const side of [-1, 1]) {
        const x = fx + side * R * 0.52;
        const y = fy + R * 0.16;
        const g = ctx.createRadialGradient(x, y, 0, x, y, R * 0.18);
        g.addColorStop(0, "rgba(255,120,180,0.45)");
        g.addColorStop(1, "rgba(255,120,180,0)");
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(x, y, R * 0.18, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    _thinkingDots(cx, cy, R, t) {
      const { ctx } = this;
      for (let i = 0; i < 3; i++) {
        const lift = Math.max(0, Math.sin(t * 5 - i * 0.7)) * R * 0.1;
        ctx.fillStyle = `rgba(255,255,255,${0.5 + lift / (R * 0.2)})`;
        ctx.beginPath();
        ctx.arc(cx + (i - 1) * R * 0.2, cy - R * 1.45 - lift, R * 0.055, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  /** Live 0..1 loudness of a MediaStream (RMS of the waveform). */
  function audioLevel(stream, audioContext) {
    const ctx = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    return () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      return Math.min(1, Math.sqrt(sum / data.length) * 4.5);
    };
  }

  window.DialynAvatar = DialynAvatar;
  window.dialynAudioLevel = audioLevel;
})();
