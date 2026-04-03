import type { ReactNode } from "react";
import { Stethoscope, UserRound } from "lucide-react";

type Variant = "doctor" | "patient";
type BulletColor = "teal" | "blue";

type LoginCardProps = {
  variant: Variant;
  imageSrc: string;
  imageAlt: string;
  title: string;
  description: string;
  bullets: [string, string, string];
  bulletColor: BulletColor;
  ctaLabel: string;
};

function BulletDot({ color }: { color: BulletColor }) {
  const cls =
    color === "teal"
      ? "bg-teal-400 shadow-[0_0_8px_rgba(45,212,191,0.5)]"
      : "bg-sky-400 shadow-[0_0_8px_rgba(56,189,248,0.45)]";
  return (
    <span
      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${cls}`}
      aria-hidden
    />
  );
}

export function LoginCard({
  variant,
  imageSrc,
  imageAlt,
  title,
  description,
  bullets,
  bulletColor,
  ctaLabel,
}: LoginCardProps) {
  const floatingIcon: ReactNode =
    variant === "doctor" ? (
      <Stethoscope className="h-7 w-7 text-white" strokeWidth={1.75} aria-hidden />
    ) : (
      <UserRound className="h-7 w-7 text-white" strokeWidth={1.75} aria-hidden />
    );

  const buttonClass =
    variant === "doctor"
      ? "bg-btn-doctor shadow-[0_8px_24px_-4px_rgba(20,184,166,0.45)] hover:shadow-[0_12px_32px_-4px_rgba(14,165,233,0.5)] hover:brightness-110"
      : "bg-btn-patient shadow-[0_8px_24px_-4px_rgba(37,99,235,0.45)] hover:shadow-[0_12px_32px_-4px_rgba(59,130,246,0.55)] hover:brightness-110";

  return (
    <article
      className="group relative flex flex-col overflow-hidden rounded-[14px] border border-white/[0.08] bg-white/[0.06] shadow-glass backdrop-blur-xl transition-all duration-300 ease-out hover:-translate-y-1 hover:border-sky-400/20 hover:shadow-glass-hover"
    >
      {/* Image region */}
      <div className="relative h-44 overflow-hidden sm:h-48">
        <img
          src={imageSrc}
          alt={imageAlt}
          className="h-full w-full object-cover transition-transform duration-500 ease-out group-hover:scale-[1.03]"
        />
        <div className="absolute inset-0 bg-gradient-to-t from-[#0a1020]/90 via-transparent to-transparent" />
      </div>

      {/* Floating icon — overlaps image + body */}
      <div className="relative z-10 -mt-7 flex justify-center px-6">
        <div
          className="flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-sky-500 to-blue-600 shadow-lg shadow-sky-900/50 ring-4 ring-[#0c1222]/80 transition-transform duration-300 group-hover:scale-105"
          aria-hidden
        >
          {floatingIcon}
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-1 flex-col px-6 pb-7 pt-2 text-center">
        <h2 className="font-display text-xl font-bold tracking-tight text-white sm:text-[1.35rem]">
          {title}
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-slate-400">
          {description}
        </p>

        <ul className="mt-6 space-y-3 text-left">
          {bullets.map((line) => (
            <li key={line} className="flex gap-3 text-sm text-slate-300">
              <BulletDot color={bulletColor} />
              <span className="leading-snug">{line}</span>
            </li>
          ))}
        </ul>

        <div className="mt-8 flex-1" />
        <button
          type="button"
          className={`w-full rounded-xl px-5 py-3.5 text-center text-sm font-semibold text-white transition-all duration-300 ${buttonClass}`}
        >
          {ctaLabel}
        </button>
      </div>
    </article>
  );
}
