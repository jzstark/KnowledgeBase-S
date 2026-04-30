"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import StarfieldCanvas from "@/app/components/StarfieldCanvas";
import styles from "./login.module.css";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (res.ok) {
        router.push("/");
        router.refresh();
      } else if (res.status === 401) {
        setError("Access denied — incorrect passphrase");
      } else {
        setError(`Service error (${res.status})`);
      }
    } catch {
      setError("Cannot reach server");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={styles.root}>
      <StarfieldCanvas className={styles.stars} />
      <div className={styles.vignette} />

      <div className={styles.card}>
        {/* Brand mark */}
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

        <div className={styles.divider} />

        <form className={styles.form} onSubmit={handleSubmit}>
          <div>
            <div className={styles.fieldLabel}>Passphrase</div>
            <div className={styles.inputWrap}>
              <input
                className={styles.input}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="· · · · · · · ·"
                required
                autoFocus
              />
            </div>
          </div>

          {error && <p className={styles.error}>{error}</p>}

          <button
            className={styles.submitBtn}
            type="submit"
            disabled={loading || !password}
          >
            {loading ? "Verifying" : "Enter"}
          </button>
        </form>

        <Link href="/" className={styles.backLink}>
          ← Return
        </Link>
      </div>
    </div>
  );
}
