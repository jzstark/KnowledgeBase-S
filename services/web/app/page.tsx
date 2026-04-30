"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import StarfieldCanvas from "@/app/components/StarfieldCanvas";
import styles from "./landing.module.css";

export default function LandingPage() {
  const graphRef = useRef<HTMLCanvasElement>(null);
  const labelRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  // Auth state: null = checking, true = logged in, false = logged out
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  // Check auth on mount
  useEffect(() => {
    fetch("/api/auth/me")
      .then((r) => setAuthed(r.ok))
      .catch(() => setAuthed(false));
  }, []);

  // Close dropdown on outside click
  useEffect(() => {
    if (!dropdownOpen) return;
    function onOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [dropdownOpen]);

  const handleLogout = useCallback(async () => {
    setDropdownOpen(false);
    await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
    setAuthed(false);
    router.refresh();
  }, [router]);

  // Swan knowledge graph animation
  useEffect(() => {
    const canvas = graphRef.current;
    const labelEl = labelRef.current;
    if (!canvas || !labelEl) return;
    const ctx = canvas.getContext("2d")!;

    let W = 0, H = 0, DPR = 1;
    let cx = 0, cy = 0, scale = 0;
    let rafId = 0;
    let intervalId: ReturnType<typeof setInterval>;

    const ANCHORS = [
      { id: "beak",    x: -0.82, y: -0.15, label: "Inference" },
      { id: "head",    x: -0.67, y: -0.16, label: "Ontology" },
      { id: "neckU",   x: -0.48, y: -0.20, label: "Entities" },
      { id: "neckM",   x: -0.31, y: -0.10, label: "Relations" },
      { id: "neckL",   x: -0.15, y:  0.00, label: "Embeddings" },
      { id: "chest",   x: -0.13, y:  0.03, label: "Reasoning" },
      { id: "core",    x:  0.02, y:  0.17, label: "Memory" },
      { id: "belly",   x: -0.12, y:  0.30, label: "Knowledge" },
      { id: "tail",    x:  0.09, y:  0.43, label: "Provenance" },
      { id: "wingN_r", x: -0.05, y: -0.03, label: "Concepts" },
      { id: "wingN_m", x: -0.10, y: -0.30, label: "Schema" },
      { id: "wingN_p", x: -0.12, y: -0.60, label: "Vectors" },
      { id: "wingN_t", x:  0.14, y: -0.41, label: "Symbols" },
      { id: "wingF_r", x:  0.34, y: -0.17, label: "Context" },
      { id: "wingF_b", x:  0.50, y:  0.05, label: "Sources" },
      { id: "wingF_m", x:  0.60, y:  0.14, label: "Corpora" },
      { id: "wingF_t", x:  0.42, y:  0.36, label: "Citations" },
      { id: "wingF_e", x:  1.05, y: -0.50, label: "Documents" },
      { id: "wingF_u", x:  0.70, y: -0.30, label: "Records" },
    ];

    const SKELETON_EDGES: [string, string][] = [
      ["beak","head"], ["head","neckU"], ["neckU","neckM"], ["neckM","neckL"],
      ["neckL","chest"], ["neckM","wingN_r"],
      ["chest","core"], ["core","belly"], ["belly","tail"], ["core","tail"],
      ["chest","belly"], ["neckL","core"],
      ["wingN_r","wingN_m"], ["wingN_m","wingN_p"], ["wingN_p","wingN_t"],
      ["wingN_m","wingN_t"], ["wingN_r","wingN_t"],
      ["chest","wingN_r"], ["core","wingN_r"],
      ["wingF_r","wingF_b"], ["wingF_b","wingF_m"], ["wingF_m","wingF_t"],
      ["wingF_r","wingF_u"], ["wingF_u","wingF_e"], ["wingF_u","wingF_b"],
      ["wingF_b","wingF_t"],
      ["core","wingF_r"], ["core","wingF_b"],
      ["wingN_t","wingF_r"], ["wingN_t","wingF_u"],
    ];

    const SATELLITE_LABELS = [
      "graph","llm","rag","agent","token","prompt","vector","index",
      "query","match","infer","prior","model","recall","signal","cluster",
      "theta","sigma","alpha","phi","omega","prime","epoch","axiom",
    ];

    type Node = {
      type: "anchor" | "satellite";
      x: number; y: number; baseX: number; baseY: number;
      vx: number; vy: number; r: number; brightness: number;
      label: string; id?: string; anchorIdx: number;
      phase: number; floatAmp: number; floatSpeed: number;
      orbitR?: number; orbitAngle?: number; orbitSpeed?: number;
    };
    type Edge = { a: number; b: number; type: "skeleton"|"satellite"|"whisker"; baseAlpha: number };
    type Pulse = { a: number; b: number; t: number; speed: number };

    let nodes: Node[] = [];
    let edges: Edge[] = [];
    const pulses: Pulse[] = [];

    function project(nx: number, ny: number): [number, number] {
      return [cx + nx * scale, cy + ny * scale];
    }

    function rebuild() {
      nodes = []; edges = [];
      ANCHORS.forEach((a, i) => {
        const [x, y] = project(a.x, a.y);
        nodes.push({
          type: "anchor", x, y, baseX: x, baseY: y, vx: 0, vy: 0,
          r: 2.6 * DPR, brightness: 1, label: a.label, id: a.id,
          anchorIdx: i, phase: Math.random() * Math.PI * 2,
          floatAmp: 4 * DPR + Math.random() * 3 * DPR,
          floatSpeed: 0.0003 + Math.random() * 0.0004,
        });
      });
      const idToIdx: Record<string, number> = {};
      nodes.forEach((n, i) => { if (n.id) idToIdx[n.id] = i; });
      SKELETON_EDGES.forEach(([a, b]) => {
        edges.push({ a: idToIdx[a], b: idToIdx[b], type: "skeleton", baseAlpha: 0.32 });
      });
      ANCHORS.forEach((_a, i) => {
        const [ax, ay] = project(_a.x, _a.y);
        const count = 3 + Math.floor(Math.random() * 3);
        const slot = (Math.PI * 2) / count;
        const startAng = Math.random() * Math.PI * 2;
        const satIdxs: number[] = [];
        for (let k = 0; k < count; k++) {
          const angle = startAng + slot * k + (Math.random() - 0.5) * slot * 0.3;
          const dist = (16 + Math.random() * 10) * DPR;
          const idx = nodes.length;
          satIdxs.push(idx);
          nodes.push({
            type: "satellite",
            x: ax + Math.cos(angle) * dist, y: ay + Math.sin(angle) * dist,
            baseX: ax + Math.cos(angle) * dist, baseY: ay + Math.sin(angle) * dist,
            vx: 0, vy: 0,
            r: (Math.random() * 1.0 + 0.6) * DPR, brightness: 0.4 + Math.random() * 0.4,
            label: SATELLITE_LABELS[Math.floor(Math.random() * SATELLITE_LABELS.length)],
            anchorIdx: i, phase: Math.random() * Math.PI * 2,
            floatAmp: 2 * DPR + Math.random() * 4 * DPR,
            floatSpeed: 0.0004 + Math.random() * 0.0008,
            orbitR: dist, orbitAngle: angle,
            orbitSpeed: (Math.random() < 0.5 ? -1 : 1) * (0.00008 + Math.random() * 0.00018),
          });
          edges.push({ a: i, b: idx, type: "satellite", baseAlpha: 0.1 + Math.random() * 0.08 });
        }
        for (let k = 0; k < satIdxs.length; k++) {
          if (Math.random() < 0.55) {
            edges.push({ a: satIdxs[k], b: satIdxs[(k + 1) % satIdxs.length], type: "whisker", baseAlpha: 0.05 });
          }
        }
      });
    }

    function resize() {
      DPR = Math.min(window.devicePixelRatio || 1, 2);
      W = canvas!.width = window.innerWidth * DPR;
      H = canvas!.height = window.innerHeight * DPR;
      canvas!.style.width = window.innerWidth + "px";
      canvas!.style.height = window.innerHeight + "px";
      cx = W * 0.58; cy = H * 0.52;
      scale = Math.min(W * 0.26, H * 0.46);
      rebuild();
    }

    let mouseX = -9999, mouseY = -9999, hoverIdx = -1;
    function onMouseMove(e: MouseEvent) { mouseX = e.clientX * DPR; mouseY = e.clientY * DPR; }
    function onMouseLeave() { mouseX = -9999; mouseY = -9999; }

    const swanImg = new Image();
    swanImg.src = "/swan-outline.png";
    let swanImgReady = false;
    swanImg.onload = () => { swanImgReady = true; };

    function drawSilhouette(t: number) {
      if (!swanImgReady) return;
      const breath = 0.5 + 0.5 * Math.sin(t * 0.0005);
      const baseAlpha = 0.42 + 0.1 * breath;
      const drawW = 2.4 * scale;
      const drawH = drawW * (swanImg.height / swanImg.width);
      const dx = cx - drawW / 2, dy = cy - drawH / 2;
      ctx.save(); ctx.globalAlpha = baseAlpha * 0.55;
      ctx.filter = `blur(${4 * DPR}px)`;
      ctx.drawImage(swanImg, dx, dy, drawW, drawH);
      ctx.restore();
      ctx.save(); ctx.globalAlpha = baseAlpha;
      ctx.drawImage(swanImg, dx, dy, drawW, drawH);
      ctx.restore();
    }

    function spawnPulse() {
      const sk = edges.filter((e) => e.type === "skeleton");
      if (!sk.length) return;
      const e = sk[Math.floor(Math.random() * sk.length)];
      pulses.push({ a: e.a, b: e.b, t: 0, speed: 0.004 + Math.random() * 0.005 });
    }

    function draw(t: number) {
      ctx.clearRect(0, 0, W, H);
      drawSilhouette(t);
      for (const n of nodes) {
        if (n.type === "satellite" && n.orbitAngle !== undefined && n.orbitSpeed !== undefined && n.orbitR !== undefined) {
          n.orbitAngle += n.orbitSpeed * 16;
          const anchor = nodes[n.anchorIdx];
          n.baseX = anchor.baseX + Math.cos(n.orbitAngle) * n.orbitR;
          n.baseY = anchor.baseY + Math.sin(n.orbitAngle) * n.orbitR;
        }
        const floatX = Math.sin(t * n.floatSpeed + n.phase) * n.floatAmp * 0.4;
        const floatY = Math.cos(t * n.floatSpeed * 1.1 + n.phase) * n.floatAmp * 0.4;
        let mx = 0, my = 0;
        if (n.type === "satellite") {
          const dx = n.baseX + floatX - mouseX, dy = n.baseY + floatY - mouseY;
          const d2 = dx * dx + dy * dy, R = 120 * DPR;
          if (d2 < R * R) {
            const d = Math.sqrt(d2) || 1, force = (1 - d / R) * 22 * DPR;
            mx = (dx / d) * force; my = (dy / d) * force;
          }
        }
        n.x = n.baseX + floatX + mx; n.y = n.baseY + floatY + my;
      }
      hoverIdx = -1; let bestD = 24 * DPR;
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        if (n.type !== "anchor") continue;
        const d = Math.hypot(n.x - mouseX, n.y - mouseY);
        if (d < bestD) { bestD = d; hoverIdx = i; }
      }
      for (const e of edges) {
        const a = nodes[e.a], b = nodes[e.b];
        if (!a || !b) continue;
        let alpha = e.baseAlpha;
        if (hoverIdx === e.a || hoverIdx === e.b) alpha = Math.min(0.7, alpha + 0.4);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = `rgba(180, 200, 240, ${alpha})`;
        ctx.lineWidth = (e.type === "skeleton" ? 0.7 : 0.4) * DPR;
        ctx.stroke();
      }
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i]; p.t += p.speed;
        if (p.t > 1) { pulses.splice(i, 1); continue; }
        const a = nodes[p.a], b = nodes[p.b];
        if (!a || !b) continue;
        const x = a.x + (b.x - a.x) * p.t, y = a.y + (b.y - a.y) * p.t;
        const grad = ctx.createRadialGradient(x, y, 0, x, y, 8 * DPR);
        grad.addColorStop(0, "rgba(180, 210, 255, 0.9)");
        grad.addColorStop(1, "rgba(180, 210, 255, 0)");
        ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(x, y, 8 * DPR, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "rgba(230, 240, 255, 0.95)";
        ctx.beginPath(); ctx.arc(x, y, 1.2 * DPR, 0, Math.PI * 2); ctx.fill();
      }
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i]; const isHover = i === hoverIdx;
        const r = n.r * (isHover ? 1.6 : 1), b = n.brightness * (isHover ? 1.3 : 1);
        const haloR = r * (n.type === "anchor" ? 7 : 4);
        const grad = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, haloR);
        grad.addColorStop(0, `rgba(200, 215, 255, ${(n.type === "anchor" ? 0.35 : 0.18) * b})`);
        grad.addColorStop(1, "rgba(200, 215, 255, 0)");
        ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(n.x, n.y, haloR, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = n.type === "anchor" ? `rgba(235, 240, 255, ${0.95 * b})` : `rgba(210, 220, 245, ${0.85 * b})`;
        ctx.fill();
        if (n.type === "anchor") {
          ctx.beginPath(); ctx.arc(n.x, n.y, r + 3 * DPR, 0, Math.PI * 2);
          ctx.strokeStyle = `rgba(180, 200, 245, ${0.3 + (isHover ? 0.4 : 0)})`;
          ctx.lineWidth = 0.5 * DPR; ctx.stroke();
        }
      }
      if (hoverIdx >= 0) {
        const n = nodes[hoverIdx];
        labelEl!.textContent = n.label;
        labelEl!.style.left = n.x / DPR + 16 + "px";
        labelEl!.style.top = n.y / DPR - 14 + "px";
        labelEl!.style.opacity = "1";
      } else {
        labelEl!.style.opacity = "0";
      }
      rafId = requestAnimationFrame(draw);
    }

    window.addEventListener("resize", resize);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseleave", onMouseLeave);
    resize();
    intervalId = setInterval(spawnPulse, 900);
    rafId = requestAnimationFrame(draw);
    return () => {
      window.removeEventListener("resize", resize);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseleave", onMouseLeave);
      cancelAnimationFrame(rafId);
      clearInterval(intervalId);
    };
  }, []);

  return (
    <div className={styles.root}>
      <div className={styles.stage}>
        <StarfieldCanvas className={styles.layer} />
        <canvas className={styles.layer} ref={graphRef} />
      </div>

      <div className={styles.vignette} />
      <div className={styles.grain} />

      <header className={styles.header}>
        <div className={styles.brand}>
          <div className={styles.brandMark}>
            <svg viewBox="-14 -14 28 28">
              <g className={styles.pulse}>
                <circle cx="0" cy="-8" r="1.4" fill="#e8eaf2" />
                <circle cx="-7" cy="-2" r="1" fill="#e8eaf2" opacity="0.7" />
                <circle cx="7" cy="-2" r="1" fill="#e8eaf2" opacity="0.7" />
                <circle cx="-4" cy="6" r="1" fill="#e8eaf2" opacity="0.5" />
                <circle cx="4" cy="6" r="1" fill="#e8eaf2" opacity="0.5" />
                <circle cx="0" cy="0" r="1.6" fill="oklch(0.85 0.05 240)" />
                <line x1="0" y1="-8" x2="-7" y2="-2" stroke="#e8eaf2" strokeWidth="0.3" opacity="0.4" />
                <line x1="0" y1="-8" x2="7" y2="-2" stroke="#e8eaf2" strokeWidth="0.3" opacity="0.4" />
                <line x1="-7" y1="-2" x2="-4" y2="6" stroke="#e8eaf2" strokeWidth="0.3" opacity="0.4" />
                <line x1="7" y1="-2" x2="4" y2="6" stroke="#e8eaf2" strokeWidth="0.3" opacity="0.4" />
                <line x1="0" y1="0" x2="0" y2="-8" stroke="oklch(0.85 0.05 240)" strokeWidth="0.3" opacity="0.6" />
                <line x1="0" y1="0" x2="-7" y2="-2" stroke="oklch(0.85 0.05 240)" strokeWidth="0.3" opacity="0.4" />
                <line x1="0" y1="0" x2="7" y2="-2" stroke="oklch(0.85 0.05 240)" strokeWidth="0.3" opacity="0.4" />
              </g>
            </svg>
          </div>
          <div className={styles.brandName}>
            Swan <em>Knowledge Graph</em>
          </div>
        </div>

        <nav className={styles.nav}>
          <a href="#" className={`${styles.navLink} ${styles.navLinkActive}`}>Home</a>
          <a href="#" className={styles.navLink}>About</a>
          <a href="#" className={styles.navLink}>Contact</a>

          {/* Show nothing while auth state is loading to avoid flash */}
          {authed === false && (
            <Link href="/login" className={`${styles.navLink} ${styles.loginBtn}`}>
              Login
            </Link>
          )}

          {authed === true && (
            <div
              className={styles.userSection}
              ref={dropdownRef}
              onClick={() => setDropdownOpen((v) => !v)}
            >
              <div className={styles.avatar}>S</div>
              <span className={styles.userName}>Swanny</span>
              <span className={`${styles.chevron} ${dropdownOpen ? styles.chevronOpen : ""}`}>
                ▾
              </span>

              {dropdownOpen && (
                <div className={styles.dropdown}>
                  <Link
                    href="/briefing"
                    className={styles.dropdownItem}
                    onClick={() => setDropdownOpen(false)}
                  >
                    <span className={styles.dropdownItemIcon}>⌘</span>
                    Console
                  </Link>
                  <div className={styles.dropdownDivider} />
                  <button
                    className={`${styles.dropdownItem} ${styles.dropdownItemDanger}`}
                    onClick={handleLogout}
                  >
                    <span className={styles.dropdownItemIcon}>↩</span>
                    Sign out
                  </button>
                </div>
              )}
            </div>
          )}
        </nav>
      </header>

      <div className={styles.hero}>
        <h1 className={styles.heroH1}>
          Connect dots looking <em>forward</em>.
        </h1>
        <p className={styles.heroP}>
          An LLM-native knowledge graph that connects ideas the way a stargazer
          connects light — quietly, patiently, until a shape appears.
        </p>
        <div className={styles.heroActions}>
          {authed ? (
            <Link href="/briefing" className={`${styles.cta} ${styles.ctaPrimary}`}>
              Open Console
            </Link>
          ) : (
            <Link href="/login" className={`${styles.cta} ${styles.ctaPrimary}`}>
              Begin Mapping
            </Link>
          )}
          <a href="#" className={`${styles.cta} ${styles.ctaGhost}`}>
            Watch Demo
          </a>
        </div>
      </div>

      <div className={styles.rail}>
        <div className={styles.coord}>
          <span>RA 19h 30m · DEC +40°</span>
          <span>Cygnus / The Swan</span>
        </div>
        <div className={styles.meta}>
          <span>
            <span className={styles.liveDot} />
            Graph synthesizing — 1,284 nodes
          </span>
          <span>v0.1 · Apr 2026</span>
        </div>
      </div>

      <div className={styles.nodeLabel} ref={labelRef} />
    </div>
  );
}
