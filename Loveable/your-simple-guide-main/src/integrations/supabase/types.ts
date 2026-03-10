export type Json =
  | string
  | number
  | boolean
  | null
  | { [key: string]: Json | undefined }
  | Json[]

export type Database = {
  // Allows to automatically instantiate createClient with right options
  // instead of createClient<Database, { PostgrestVersion: 'XX' }>(URL, KEY)
  __InternalSupabase: {
    PostgrestVersion: "14.1"
  }
  public: {
    Tables: {
      control_log: {
        Row: {
          action: string
          created_at: string
          dewpoint: number | null
          dp_spread: number | null
          energy_saved_pct: number | null
          forecast_best_hour: string | null
          forecast_temp_max: number | null
          heating_minutes_today: number | null
          humidity_airzone: number | null
          humidity_netatmo: number | null
          id: string
          occupancy_detected: boolean | null
          outdoor_humidity: number | null
          outdoor_temp: number | null
          reason: string | null
          success: boolean | null
          temperature: number | null
          zone_name: string
        }
        Insert: {
          action: string
          created_at?: string
          dewpoint?: number | null
          dp_spread?: number | null
          energy_saved_pct?: number | null
          forecast_best_hour?: string | null
          forecast_temp_max?: number | null
          heating_minutes_today?: number | null
          humidity_airzone?: number | null
          humidity_netatmo?: number | null
          id?: string
          occupancy_detected?: boolean | null
          outdoor_humidity?: number | null
          outdoor_temp?: number | null
          reason?: string | null
          success?: boolean | null
          temperature?: number | null
          zone_name: string
        }
        Update: {
          action?: string
          created_at?: string
          dewpoint?: number | null
          dp_spread?: number | null
          energy_saved_pct?: number | null
          forecast_best_hour?: string | null
          forecast_temp_max?: number | null
          heating_minutes_today?: number | null
          humidity_airzone?: number | null
          humidity_netatmo?: number | null
          id?: string
          occupancy_detected?: boolean | null
          outdoor_humidity?: number | null
          outdoor_temp?: number | null
          reason?: string | null
          success?: boolean | null
          temperature?: number | null
          zone_name?: string
        }
        Relationships: []
      }
      daily_assessment: {
        Row: {
          actual_kwh: number | null
          avg_humidity_after: number | null
          avg_humidity_before: number | null
          correction_factor: number | null
          created_at: string
          date: string
          estimation_accuracy_pct: number | null
          heating_minutes: number | null
          humidity_improved: boolean | null
          id: string
          notes: string | null
          occupancy_detected: boolean | null
          total_cost_eur: number | null
          total_heating_kwh: number | null
          ventilation_suggestions: number | null
          zones_above_65: number | null
          zones_total: number | null
        }
        Insert: {
          actual_kwh?: number | null
          avg_humidity_after?: number | null
          avg_humidity_before?: number | null
          correction_factor?: number | null
          created_at?: string
          date: string
          estimation_accuracy_pct?: number | null
          heating_minutes?: number | null
          humidity_improved?: boolean | null
          id?: string
          notes?: string | null
          occupancy_detected?: boolean | null
          total_cost_eur?: number | null
          total_heating_kwh?: number | null
          ventilation_suggestions?: number | null
          zones_above_65?: number | null
          zones_total?: number | null
        }
        Update: {
          actual_kwh?: number | null
          avg_humidity_after?: number | null
          avg_humidity_before?: number | null
          correction_factor?: number | null
          created_at?: string
          date?: string
          estimation_accuracy_pct?: number | null
          heating_minutes?: number | null
          humidity_improved?: boolean | null
          id?: string
          notes?: string | null
          occupancy_detected?: boolean | null
          total_cost_eur?: number | null
          total_heating_kwh?: number | null
          ventilation_suggestions?: number | null
          zones_above_65?: number | null
          zones_total?: number | null
        }
        Relationships: []
      }
      dp_spread_predictions: {
        Row: {
          actual_dp_spread: number | null
          actual_indoor_temp: number | null
          created_at: string
          current_dp_spread: number | null
          current_indoor_temp: number | null
          current_outdoor_temp: number | null
          decision_correct: boolean | null
          decision_made: string | null
          hours_ahead: number
          id: string
          predicted_dp_spread: number
          predicted_for: string
          predicted_indoor_temp: number | null
          predicted_outdoor_humidity: number | null
          predicted_outdoor_temp: number | null
          prediction_error: number | null
          validated: boolean
          validated_at: string | null
          zone_name: string
        }
        Insert: {
          actual_dp_spread?: number | null
          actual_indoor_temp?: number | null
          created_at?: string
          current_dp_spread?: number | null
          current_indoor_temp?: number | null
          current_outdoor_temp?: number | null
          decision_correct?: boolean | null
          decision_made?: string | null
          hours_ahead?: number
          id?: string
          predicted_dp_spread: number
          predicted_for: string
          predicted_indoor_temp?: number | null
          predicted_outdoor_humidity?: number | null
          predicted_outdoor_temp?: number | null
          prediction_error?: number | null
          validated?: boolean
          validated_at?: string | null
          zone_name: string
        }
        Update: {
          actual_dp_spread?: number | null
          actual_indoor_temp?: number | null
          created_at?: string
          current_dp_spread?: number | null
          current_indoor_temp?: number | null
          current_outdoor_temp?: number | null
          decision_correct?: boolean | null
          decision_made?: string | null
          hours_ahead?: number
          id?: string
          predicted_dp_spread?: number
          predicted_for?: string
          predicted_indoor_temp?: number | null
          predicted_outdoor_humidity?: number | null
          predicted_outdoor_temp?: number | null
          prediction_error?: number | null
          validated?: boolean
          validated_at?: string | null
          zone_name?: string
        }
        Relationships: []
      }
      energy_baseline: {
        Row: {
          baseline_wh: number
          dhw_active_avg_wh: number | null
          hour_of_day: number
          id: string
          last_updated: string | null
          notes: string | null
          sample_count: number
        }
        Insert: {
          baseline_wh?: number
          dhw_active_avg_wh?: number | null
          hour_of_day: number
          id?: string
          last_updated?: string | null
          notes?: string | null
          sample_count?: number
        }
        Update: {
          baseline_wh?: number
          dhw_active_avg_wh?: number | null
          hour_of_day?: number
          id?: string
          last_updated?: string | null
          notes?: string | null
          sample_count?: number
        }
        Relationships: []
      }
      heating_experiments: {
        Row: {
          avg_humidity_after: number | null
          avg_humidity_before: number | null
          avg_humidity_during: number | null
          avg_indoor_temp: number | null
          avg_outdoor_humidity: number | null
          avg_outdoor_temp: number | null
          completed_at: string | null
          conclusion: string | null
          created_at: string
          end_date: string
          id: string
          reason: string | null
          recommendation: string | null
          start_date: string
          status: string
          thermal_runoff_hours: number | null
          type: string
        }
        Insert: {
          avg_humidity_after?: number | null
          avg_humidity_before?: number | null
          avg_humidity_during?: number | null
          avg_indoor_temp?: number | null
          avg_outdoor_humidity?: number | null
          avg_outdoor_temp?: number | null
          completed_at?: string | null
          conclusion?: string | null
          created_at?: string
          end_date: string
          id?: string
          reason?: string | null
          recommendation?: string | null
          start_date: string
          status?: string
          thermal_runoff_hours?: number | null
          type?: string
        }
        Update: {
          avg_humidity_after?: number | null
          avg_humidity_before?: number | null
          avg_humidity_during?: number | null
          avg_indoor_temp?: number | null
          avg_outdoor_humidity?: number | null
          avg_outdoor_temp?: number | null
          completed_at?: string | null
          conclusion?: string | null
          created_at?: string
          end_date?: string
          id?: string
          reason?: string | null
          recommendation?: string | null
          start_date?: string
          status?: string
          thermal_runoff_hours?: number | null
          type?: string
        }
        Relationships: []
      }
      netatmo_readings: {
        Row: {
          co2: number | null
          created_at: string | null
          humidity: number | null
          id: string
          module_name: string
          noise: number | null
          pressure: number | null
          temperature: number | null
          timestamp: string
        }
        Insert: {
          co2?: number | null
          created_at?: string | null
          humidity?: number | null
          id?: string
          module_name: string
          noise?: number | null
          pressure?: number | null
          temperature?: number | null
          timestamp: string
        }
        Update: {
          co2?: number | null
          created_at?: string | null
          humidity?: number | null
          id?: string
          module_name?: string
          noise?: number | null
          pressure?: number | null
          temperature?: number | null
          timestamp?: string
        }
        Relationships: []
      }
      netatmo_sync_status: {
        Row: {
          device_id: string
          last_synced_ts: number
          module_id: string | null
          module_name: string
          module_type: string
          status: string
          updated_at: string | null
        }
        Insert: {
          device_id: string
          last_synced_ts?: number
          module_id?: string | null
          module_name: string
          module_type: string
          status?: string
          updated_at?: string | null
        }
        Update: {
          device_id?: string
          last_synced_ts?: number
          module_id?: string | null
          module_name?: string
          module_type?: string
          status?: string
          updated_at?: string | null
        }
        Relationships: []
      }
      system_state: {
        Row: {
          key: string
          updated_at: string
          value: Json
        }
        Insert: {
          key: string
          updated_at?: string
          value?: Json
        }
        Update: {
          key?: string
          updated_at?: string
          value?: Json
        }
        Relationships: []
      }
      tariff_rates: {
        Row: {
          created_at: string | null
          fixed_annual_eur: number
          id: string
          notes: string | null
          valid_from: string
          variable_rate_kwh: number
        }
        Insert: {
          created_at?: string | null
          fixed_annual_eur: number
          id?: string
          notes?: string | null
          valid_from: string
          variable_rate_kwh: number
        }
        Update: {
          created_at?: string | null
          fixed_annual_eur?: number
          id?: string
          notes?: string | null
          valid_from?: string
          variable_rate_kwh?: number
        }
        Relationships: []
      }
    }
    Views: {
      [_ in never]: never
    }
    Functions: {
      [_ in never]: never
    }
    Enums: {
      [_ in never]: never
    }
    CompositeTypes: {
      [_ in never]: never
    }
  }
}

type DatabaseWithoutInternals = Omit<Database, "__InternalSupabase">

type DefaultSchema = DatabaseWithoutInternals[Extract<keyof Database, "public">]

export type Tables<
  DefaultSchemaTableNameOrOptions extends
    | keyof (DefaultSchema["Tables"] & DefaultSchema["Views"])
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
        DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? (DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"] &
      DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Views"])[TableName] extends {
      Row: infer R
    }
    ? R
    : never
  : DefaultSchemaTableNameOrOptions extends keyof (DefaultSchema["Tables"] &
        DefaultSchema["Views"])
    ? (DefaultSchema["Tables"] &
        DefaultSchema["Views"])[DefaultSchemaTableNameOrOptions] extends {
        Row: infer R
      }
      ? R
      : never
    : never

export type TablesInsert<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Insert: infer I
    }
    ? I
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Insert: infer I
      }
      ? I
      : never
    : never

export type TablesUpdate<
  DefaultSchemaTableNameOrOptions extends
    | keyof DefaultSchema["Tables"]
    | { schema: keyof DatabaseWithoutInternals },
  TableName extends DefaultSchemaTableNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"]
    : never = never,
> = DefaultSchemaTableNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaTableNameOrOptions["schema"]]["Tables"][TableName] extends {
      Update: infer U
    }
    ? U
    : never
  : DefaultSchemaTableNameOrOptions extends keyof DefaultSchema["Tables"]
    ? DefaultSchema["Tables"][DefaultSchemaTableNameOrOptions] extends {
        Update: infer U
      }
      ? U
      : never
    : never

export type Enums<
  DefaultSchemaEnumNameOrOptions extends
    | keyof DefaultSchema["Enums"]
    | { schema: keyof DatabaseWithoutInternals },
  EnumName extends DefaultSchemaEnumNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"]
    : never = never,
> = DefaultSchemaEnumNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[DefaultSchemaEnumNameOrOptions["schema"]]["Enums"][EnumName]
  : DefaultSchemaEnumNameOrOptions extends keyof DefaultSchema["Enums"]
    ? DefaultSchema["Enums"][DefaultSchemaEnumNameOrOptions]
    : never

export type CompositeTypes<
  PublicCompositeTypeNameOrOptions extends
    | keyof DefaultSchema["CompositeTypes"]
    | { schema: keyof DatabaseWithoutInternals },
  CompositeTypeName extends PublicCompositeTypeNameOrOptions extends {
    schema: keyof DatabaseWithoutInternals
  }
    ? keyof DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"]
    : never = never,
> = PublicCompositeTypeNameOrOptions extends {
  schema: keyof DatabaseWithoutInternals
}
  ? DatabaseWithoutInternals[PublicCompositeTypeNameOrOptions["schema"]]["CompositeTypes"][CompositeTypeName]
  : PublicCompositeTypeNameOrOptions extends keyof DefaultSchema["CompositeTypes"]
    ? DefaultSchema["CompositeTypes"][PublicCompositeTypeNameOrOptions]
    : never

export const Constants = {
  public: {
    Enums: {},
  },
} as const
