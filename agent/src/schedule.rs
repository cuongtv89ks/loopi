//! Quiet hours: local-time ranges during which nothing is recorded.
use chrono::{DateTime, NaiveTime, Utc};
use chrono_tz::Tz;

use crate::config::ScheduleConfig;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct QuietHours {
    ranges: Vec<(NaiveTime, NaiveTime)>,
}

impl QuietHours {
    /// Parse `["HH:MM-HH:MM", ...]`. A range may wrap past midnight (`22:00-07:00`).
    pub fn parse(specs: &[String]) -> Result<Self, String> {
        let mut ranges = Vec::with_capacity(specs.len());
        for spec in specs {
            let (start, end) = spec
                .split_once('-')
                .ok_or_else(|| format!("quiet hours {spec:?} must look like HH:MM-HH:MM"))?;
            let parse = |s: &str| {
                NaiveTime::parse_from_str(s.trim(), "%H:%M").map_err(|_| format!("bad time {s:?} in {spec:?}"))
            };
            let (start, end) = (parse(start)?, parse(end)?);
            if start == end {
                return Err(format!("quiet hours {spec:?} is empty"));
            }
            ranges.push((start, end));
        }
        Ok(Self { ranges })
    }

    pub fn contains(&self, t: NaiveTime) -> bool {
        self.ranges.iter().any(|&(start, end)| {
            if start < end {
                t >= start && t < end
            } else {
                t >= start || t < end
            }
        })
    }
}

#[derive(Debug, Clone)]
pub struct Schedule {
    tz: Tz,
    quiet: QuietHours,
}

impl Schedule {
    pub fn from_config(config: &ScheduleConfig) -> Result<Self, String> {
        let tz: Tz = config
            .timezone
            .parse()
            .map_err(|_| format!("unknown timezone {:?}", config.timezone))?;
        Ok(Self { tz, quiet: QuietHours::parse(&config.quiet_hours)? })
    }

    pub fn is_quiet(&self, now: DateTime<Utc>) -> bool {
        self.quiet.contains(now.with_timezone(&self.tz).time())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn t(h: u32, m: u32) -> NaiveTime {
        NaiveTime::from_hms_opt(h, m, 0).unwrap()
    }

    #[test]
    fn wrapping_range_covers_midnight() {
        let q = QuietHours::parse(&["22:00-07:00".into()]).unwrap();
        assert!(q.contains(t(23, 0)));
        assert!(q.contains(t(6, 59)));
        assert!(!q.contains(t(7, 0)));
        assert!(!q.contains(t(12, 0)));
    }

    #[test]
    fn plain_range_is_half_open() {
        let q = QuietHours::parse(&["12:00-13:30".into()]).unwrap();
        assert!(q.contains(t(12, 0)));
        assert!(!q.contains(t(13, 30)));
    }

    #[test]
    fn invalid_specs_are_rejected() {
        for bad in ["noon", "25:00-01:00", "08:00-08:00", "8-9"] {
            assert!(QuietHours::parse(&[bad.into()]).is_err(), "{bad} should fail");
        }
    }

    #[test]
    fn schedule_uses_local_time() {
        let cfg = ScheduleConfig { timezone: "Asia/Seoul".into(), quiet_hours: vec!["22:00-07:00".into()] };
        let schedule = Schedule::from_config(&cfg).unwrap();
        // 14:30 UTC is 23:30 in Seoul.
        assert!(schedule.is_quiet(Utc.with_ymd_and_hms(2026, 10, 14, 14, 30, 0).unwrap()));
        assert!(!schedule.is_quiet(Utc.with_ymd_and_hms(2026, 10, 14, 3, 0, 0).unwrap()));
    }

    #[test]
    fn unknown_timezone_is_rejected() {
        let cfg = ScheduleConfig { timezone: "Mars/Olympus".into(), quiet_hours: vec![] };
        assert!(Schedule::from_config(&cfg).is_err());
    }
}
