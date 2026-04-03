import { LoginCard } from "./LoginCard";

const DOCTOR_IMAGE =
  "https://images.unsplash.com/photo-1579684385127-1ef15d508118?w=800&q=85&auto=format&fit=crop";
const PATIENT_IMAGE =
  "https://images.unsplash.com/photo-1516549655169-df83a0774516?w=800&q=85&auto=format&fit=crop";

function LogoMark() {
  return (
    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 to-blue-600 shadow-lg shadow-sky-500/25 ring-1 ring-white/20">
      <svg
        className="h-6 w-6 text-white"
        fill="currentColor"
        viewBox="0 0 24 24"
        aria-hidden
      >
        <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z" />
      </svg>
    </div>
  );
}

function ShieldLine() {
  return (
    <p className="mt-2 flex items-center justify-center gap-2 text-sm text-slate-400">
      <svg
        className="h-4 w-4 shrink-0 text-sky-400/80"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={1.5}
        aria-hidden
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"
        />
      </svg>
      Your health data, protected and portable.
    </p>
  );
}

export function MedVaultLogin() {
  return (
    <div className="min-h-screen bg-page-dark px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
      <div className="mx-auto max-w-5xl">
        {/* Header */}
        <header className="mb-10 text-center sm:mb-12">
          <div className="flex flex-col items-center gap-4 sm:flex-row sm:justify-center sm:gap-5">
            <LogoMark />
            <div className="text-center sm:text-left">
              <h1 className="font-display text-3xl font-extrabold tracking-tight text-white sm:text-4xl">
                <span className="bg-gradient-to-r from-sky-400 to-indigo-400 bg-clip-text text-transparent">
                  MedVault
                </span>
              </h1>
              <p className="mt-1 text-base font-medium text-slate-300">
                Secure Medical History Vault
              </p>
            </div>
          </div>
          <ShieldLine />
        </header>

        {/* Cards */}
        <div className="grid gap-8 md:grid-cols-2 md:gap-10">
          <LoginCard
            variant="doctor"
            imageSrc={DOCTOR_IMAGE}
            imageAlt="Stethoscope and medical care"
            title="Doctor Login"
            description="Access patient records and medical data"
            bullets={[
              "View and update patient records",
              "Prescribe medications",
              "Access medical history",
            ]}
            bulletColor="teal"
            ctaLabel="Login as Doctor"
          />
          <LoginCard
            variant="patient"
            imageSrc={PATIENT_IMAGE}
            imageAlt="Hospital room equipment"
            title="Patient Login"
            description="View your personal health records"
            bullets={[
              "Access your medical history",
              "View prescriptions and reports",
              "Book appointments",
            ]}
            bulletColor="blue"
            ctaLabel="Login as Patient"
          />
        </div>

        {/* Footer */}
        <footer className="mt-12 text-center sm:mt-16">
          <p className="text-xs text-slate-500 sm:text-sm">
            Protected by end-to-end encryption • HIPAA Compliant
          </p>
        </footer>
      </div>
    </div>
  );
}
