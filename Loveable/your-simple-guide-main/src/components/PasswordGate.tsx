import { useState, useEffect } from "react";
import { supabase } from "@/integrations/supabase/client";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Lock } from "lucide-react";

const SESSION_KEY = "heatsmart_auth";
const SESSION_DURATION = 7 * 24 * 60 * 60 * 1000; // 7 days

interface PasswordGateProps {
  children: React.ReactNode;
}

export default function PasswordGate({ children }: PasswordGateProps) {
  const [authenticated, setAuthenticated] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem(SESSION_KEY);
    if (stored) {
      const { expiry } = JSON.parse(stored);
      if (Date.now() < expiry) {
        setAuthenticated(true);
      } else {
        localStorage.removeItem(SESSION_KEY);
      }
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const { data, error: fnError } = await supabase.functions.invoke("verify-password", {
        body: { password },
      });

      if (fnError) throw fnError;

      if (data?.valid) {
        localStorage.setItem(SESSION_KEY, JSON.stringify({ expiry: Date.now() + SESSION_DURATION }));
        setAuthenticated(true);
      } else {
        setError("Wrong password");
      }
    } catch {
      setError("Verification failed. Try again.");
    } finally {
      setLoading(false);
    }
  };

  if (authenticated) return <>{children}</>;

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-4">
      <form onSubmit={handleSubmit} className="w-full max-w-xs space-y-4 text-center">
        <div className="flex justify-center">
          <div className="h-12 w-12 rounded-xl bg-gradient-to-br from-heating to-heating-glow flex items-center justify-center">
            <Lock className="h-6 w-6 text-background" />
          </div>
        </div>
        <h1 className="text-lg font-bold text-foreground">HeatSmart</h1>
        <p className="text-xs text-muted-foreground">Enter password to access the dashboard</p>
        <Input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        {error && <p className="text-xs text-destructive">{error}</p>}
        <Button type="submit" className="w-full" disabled={loading || !password}>
          {loading ? "Checking..." : "Enter"}
        </Button>
      </form>
    </div>
  );
}
