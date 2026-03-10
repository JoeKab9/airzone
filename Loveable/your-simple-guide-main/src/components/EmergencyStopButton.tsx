import { useState } from "react";
import { Button } from "@/components/ui/button";
import { supabase } from "@/integrations/supabase/client";
import { useSystemState } from "@/hooks/useControlData";
import { OctagonX, Play } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

const EmergencyStopButton = () => {
  const [loading, setLoading] = useState(false);
  const { data: systemState } = useSystemState();
  const queryClient = useQueryClient();

  const isEmergencyStopped = systemState?.emergency_stop?.active === true;

  const handleAction = async () => {
    const action = isEmergencyStopped ? "resume" : "stop";
    const confirmed = action === "stop"
      ? window.confirm("⚠️ EMERGENCY STOP\n\nThis will turn off ALL heating and pause automation.\nYou can control heating manually via the Airzone app.\n\nProceed?")
      : window.confirm("Resume automation?\n\nThe system will start controlling heating again.");

    if (!confirmed) return;

    setLoading(true);
    try {
      const { error } = await supabase.functions.invoke("emergency-stop", {
        body: { action },
      });
      if (error) throw error;
      queryClient.invalidateQueries({ queryKey: ["system-state"] });
      queryClient.invalidateQueries({ queryKey: ["control-logs"] });
    } catch (e) {
      alert(`Failed: ${e instanceof Error ? e.message : "Unknown error"}`);
    } finally {
      setLoading(false);
    }
  };

  if (isEmergencyStopped) {
    return (
      <Button
        onClick={handleAction}
        disabled={loading}
        variant="outline"
        size="sm"
        className="h-8 text-xs font-bold border-success/30 bg-success/10 text-success hover:bg-success/20 hover:text-success gap-1.5"
      >
        <Play className="h-3.5 w-3.5" />
        {loading ? "Resuming…" : "Resume"}
      </Button>
    );
  }

  return (
    <Button
      onClick={handleAction}
      disabled={loading}
      size="sm"
      className="h-8 text-xs font-bold bg-destructive hover:bg-destructive/90 text-destructive-foreground gap-1.5"
    >
      <OctagonX className="h-3.5 w-3.5" />
      {loading ? "Stopping…" : "STOP"}
    </Button>
  );
};

export default EmergencyStopButton;
