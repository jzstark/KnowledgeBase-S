"use client";

import { useEffect, useRef } from "react";

export default function StarfieldCanvas({ className }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d")!;

    let W = 0, H = 0, DPR = 1;
    type Star = {
      x: number; y: number; r: number; baseAlpha: number;
      twinkleSpeed: number; phase: number; warm: boolean;
    };
    const stars: Star[] = [];
    const STAR_COUNT = 620;
    let rafId = 0;

    function seed() {
      stars.length = 0;
      for (let i = 0; i < STAR_COUNT; i++) {
        const isSparkle = Math.random() < 0.12;
        stars.push({
          x: Math.random() * W,
          y: Math.random() * H,
          r: (isSparkle ? Math.random() * 1.4 + 0.8 : Math.random() * 0.8 + 0.15) * DPR,
          baseAlpha: isSparkle ? Math.random() * 0.5 + 0.5 : Math.random() * 0.5 + 0.08,
          twinkleSpeed: Math.random() * 0.003 + 0.0004,
          phase: Math.random() * Math.PI * 2,
          warm: Math.random() < 0.08,
        });
      }
    }

    function resize() {
      DPR = Math.min(window.devicePixelRatio || 1, 2);
      W = canvas!.width = window.innerWidth * DPR;
      H = canvas!.height = window.innerHeight * DPR;
      canvas!.style.width = window.innerWidth + "px";
      canvas!.style.height = window.innerHeight + "px";
      seed();
    }

    function draw(t: number) {
      ctx.clearRect(0, 0, W, H);
      for (const s of stars) {
        const a = s.baseAlpha * (0.6 + 0.4 * Math.sin(t * s.twinkleSpeed + s.phase));
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = s.warm ? `rgba(255, 230, 200, ${a})` : `rgba(220, 230, 255, ${a})`;
        ctx.fill();
        if (s.r > 1 * DPR) {
          const grad = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, s.r * 6);
          grad.addColorStop(0, `rgba(220, 230, 255, ${a * 0.3})`);
          grad.addColorStop(1, "rgba(220, 230, 255, 0)");
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.arc(s.x, s.y, s.r * 6, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      rafId = requestAnimationFrame(draw);
    }

    window.addEventListener("resize", resize);
    resize();
    rafId = requestAnimationFrame(draw);
    return () => {
      window.removeEventListener("resize", resize);
      cancelAnimationFrame(rafId);
    };
  }, []);

  return <canvas ref={ref} className={className} />;
}
