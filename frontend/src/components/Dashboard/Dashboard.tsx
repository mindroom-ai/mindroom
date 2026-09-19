import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EntityAvatar } from "@/components/shared/EntityAvatar";
import { useConfigStore } from "@/store/configStore";
import type { Room } from "@/types/config";
import type { ScheduleTask } from "@/types/schedule";
import { WorkspaceDirectory } from "./WorkspaceDirectory";
import {
  buildRunWeek,
  scheduleTime,
  upcomingSchedules,
  useHomeData,
} from "./homeData";
import "./home.css";

function SectionLink({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  return (
    <Link className="home-link" to={to}>
      {children}
      <ArrowRight aria-hidden="true" className="h-3.5 w-3.5" />
    </Link>
  );
}

function Today({
  usage,
  activity,
}: Pick<ReturnType<typeof useHomeData>, "usage" | "activity">) {
  const report =
    !usage.isError && usage.data?.status === "ready"
      ? usage.data.report
      : undefined;
  const partial = Boolean(
    report &&
    (report.daily_coverage.unavailable_sources > 0 ||
      report.coverage.unavailable_sources > 0),
  );
  const week = report
    ? buildRunWeek(report.generated_at, report.daily_breakdown, partial)
    : [];
  const today = week[week.length - 1]?.runs;
  const max = Math.max(1, ...week.map((day) => day.runs ?? 0));
  const runtime = activity.isError ? "Unavailable" : activity.data;
  return (
    <section className="home-today" aria-labelledby="today-heading">
      <div className="home-section-heading">
        <h3 id="today-heading">Today</h3>
        <span
          className="home-runtime"
          aria-label={`Runtime: ${runtime ?? "Checking"}`}
        >
          <span
            aria-hidden="true"
            className={`home-runtime-mark ${runtime === "Working" ? "is-working" : ""}`}
          />
          {runtime ?? "Checking runtime…"}
        </span>
      </div>
      {report ? (
        <>
          <div className="home-run-count">
            <strong>
              {today == null ? "—" : today.toLocaleString()}
              {partial && today != null ? "+" : ""}
            </strong>
            <span>runs today · UTC</span>
          </div>
          <div className="home-bars" aria-label="Daily recorded runs">
            {week.map((day, index) => {
              const label = `${day.date}: ${day.runs == null ? "unavailable" : `${day.runs} runs${partial ? " (partial)" : ""}`}`;
              return (
                <div key={day.date} className="home-bar-column">
                  <div
                    role="img"
                    aria-label={label}
                    title={label}
                    className="home-bar-track"
                  >
                    <span
                      className={`home-bar ${index === 6 ? "is-current" : ""} ${day.runs == null ? "is-unknown" : ""}`}
                      style={{
                        height:
                          day.runs == null
                            ? "2px"
                            : `${Math.max(2, (day.runs / max) * 66)}px`,
                      }}
                    />
                  </div>
                  <span className={index === 6 ? "text-foreground" : ""}>
                    {new Date(`${day.date}T00:00:00Z`).toLocaleDateString(
                      "en-US",
                      {
                        weekday: "short",
                        timeZone: "UTC",
                      },
                    )}
                  </span>
                </div>
              );
            })}
          </div>
          <div className="home-chart-caption">
            <span>Last 7 days · UTC</span>
            {partial && <span className="home-partial">Partial data</span>}
          </div>
          <p className="home-report-date">
            Report date:{" "}
            {new Date(report.generated_at).toLocaleDateString("en-US", {
              month: "short",
              day: "numeric",
              year: "numeric",
              timeZone: "UTC",
            })}{" "}
            · UTC
          </p>
        </>
      ) : (
        <div className="home-usage-state" role="status">
          {usage.isError ? (
            <>
              <p>Usage unavailable</p>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void usage.refetch()}
              >
                Retry usage
              </Button>
            </>
          ) : (
            <>
              <span className="home-count-placeholder">—</span>
              <p>Preparing usage report…</p>
            </>
          )}
        </div>
      )}
      <SectionLink to="/usage">View usage</SectionLink>
    </section>
  );
}

function scheduleRoom(
  task: Pick<ScheduleTask, "room_id" | "room_alias">,
  rooms: Room[],
) {
  return rooms.find(
    (room) =>
      room.id === task.room_id ||
      room.id === task.room_alias ||
      (task.room_alias?.startsWith(`#${room.id}:`) ?? false),
  );
}

function ComingUp({
  schedules,
  rooms,
}: {
  schedules: ReturnType<typeof useHomeData>["schedules"];
  rooms: Room[];
}) {
  const data = !schedules.isError ? schedules.data : undefined;
  const tasks = data ? upcomingSchedules(data.tasks) : [];
  return (
    <section className="home-upcoming" aria-labelledby="upcoming-heading">
      <div>
        <h3 id="upcoming-heading">Coming up</h3>
        <p className="home-timezone">{data?.timezone ?? "Scheduled tasks"}</p>
      </div>
      <div className="home-schedule-list">
        {schedules.isError ? (
          <p className="home-section-empty" role="status">
            Schedules unavailable
          </p>
        ) : schedules.isPending ? (
          <p className="home-section-empty" role="status">
            Loading schedules…
          </p>
        ) : tasks.length === 0 ? (
          <p className="home-section-empty">Nothing scheduled</p>
        ) : (
          tasks.map(({ task, at }) => {
            const when = scheduleTime(at, data!.timezone);
            const room = scheduleRoom(task, rooms);
            const roomName =
              room?.display_name ?? task.room_alias ?? task.room_id;
            const title = task.description || task.message;
            return (
              <div
                className="home-schedule"
                key={`${task.room_id}:${task.task_id}`}
              >
                <time dateTime={at} className="home-schedule-time">
                  <span>{when.date}</span>
                  <strong>{when.time}</strong>
                </time>
                <div className="home-schedule-body">
                  <p className="home-schedule-title" title={title}>
                    {title}
                  </p>
                  <div className="home-schedule-room">
                    <EntityAvatar
                      kind="room"
                      id={room?.id ?? task.room_id}
                      name={roomName}
                      className="h-6 w-6 rounded-lg"
                    />
                    <span title={roomName}>{roomName}</span>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
      <SectionLink to="/schedules">All schedules</SectionLink>
    </section>
  );
}

function Home({
  onBrowse,
  headingRef,
}: {
  onBrowse: () => void;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const { config, agents, rooms, teams, selectAgent, selectRoom, selectTeam } =
    useConfigStore();
  const data = useHomeData();
  const navigate = useNavigate();
  const openEditor = (kind: "agent" | "room", id: string) => {
    selectAgent(kind === "agent" ? id : null);
    selectRoom(kind === "room" ? id : null);
    selectTeam(null);
    navigate(kind === "agent" ? "/agents" : "/rooms");
  };
  const sortedRooms = [...rooms].sort((a, b) =>
    a.display_name.localeCompare(b.display_name, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
  const sortedAgents = [...agents].sort((a, b) =>
    a.display_name.localeCompare(b.display_name, undefined, {
      numeric: true,
      sensitivity: "base",
    }),
  );
  return (
    <div className="home-page">
      <header className="home-heading">
        <div>
          <h2 ref={headingRef} tabIndex={-1}>
            Home
          </h2>
          <p>
            {new Date().toLocaleDateString("en-US", {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}
          </p>
        </div>
        <Button
          className="home-browse"
          variant="outline"
          size="sm"
          onClick={onBrowse}
        >
          <Search aria-hidden="true" className="h-4 w-4" />
          Browse workspace
        </Button>
      </header>
      <div className="home-day-surface">
        <Today usage={data.usage} activity={data.activity} />
        <ComingUp schedules={data.schedules} rooms={rooms} />
      </div>
      <section aria-labelledby="rooms-heading" className="home-rooms">
        <div className="home-section-heading">
          <h3 id="rooms-heading">
            Your rooms <span className="home-count">{rooms.length}</span>
          </h3>
          <SectionLink to="/rooms">All rooms</SectionLink>
        </div>
        <div className="home-room-grid">
          {sortedRooms.slice(0, 4).map((room) => {
            const members = [
              ...room.agents.map((id) => ({
                id,
                kind: "agent" as const,
                name:
                  agents.find((agent) => agent.id === id)?.display_name ?? id,
              })),
              ...teams
                .filter((team) => team.rooms.includes(room.id))
                .map((team) => ({
                  id: team.id,
                  kind: "team" as const,
                  name: team.display_name,
                })),
            ];
            return (
              <button
                type="button"
                key={room.id}
                className="home-room"
                aria-label={`Configure ${room.display_name}`}
                onClick={() => openEditor("room", room.id)}
              >
                <EntityAvatar
                  kind="room"
                  id={room.id}
                  name={room.display_name}
                  className="h-[52px] w-[52px] rounded-2xl text-lg"
                />
                <span className="home-room-body">
                  <span className="home-room-name" title={room.display_name}>
                    {room.display_name}
                  </span>
                  {room.description && (
                    <span
                      className="home-room-description"
                      title={room.description}
                    >
                      {room.description}
                    </span>
                  )}
                  {members.length > 0 && (
                    <span
                      className="home-room-members"
                      aria-label={`Members: ${members.map((member) => member.name).join(", ")}`}
                    >
                      {members.slice(0, 4).map((member) => (
                        <EntityAvatar
                          key={`${member.kind}:${member.id}`}
                          kind={member.kind}
                          id={member.id}
                          name={member.name}
                          className="h-[22px] w-[22px] rounded-full text-[8px] ring-2 ring-background"
                        />
                      ))}
                      {members.length > 4 && (
                        <span className="home-member-overflow">
                          +{members.length - 4}
                        </span>
                      )}
                    </span>
                  )}
                </span>
                <ArrowRight
                  aria-hidden="true"
                  className="home-room-arrow h-4 w-4"
                />
              </button>
            );
          })}
        </div>
        {rooms.length === 0 && (
          <p className="home-section-empty">
            {config
              ? "No rooms yet. Add a room to give your agents a place to work."
              : "Rooms will appear when configuration is available."}
          </p>
        )}
      </section>
      <section className="home-agents" aria-labelledby="agents-heading">
        <div className="home-section-heading">
          <h3 id="agents-heading">Agents</h3>
          <SectionLink to="/agents">All agents</SectionLink>
        </div>
        <div className="home-agent-strip">
          {sortedAgents.slice(0, 6).map((agent) => (
            <button
              key={agent.id}
              type="button"
              className="home-agent"
              aria-label={`Configure ${agent.display_name}`}
              onClick={() => openEditor("agent", agent.id)}
            >
              <EntityAvatar
                kind="agent"
                id={agent.id}
                name={agent.display_name}
                className="h-9 w-9 rounded-full"
              />
              <span title={agent.display_name}>{agent.display_name}</span>
            </button>
          ))}
        </div>
        {agents.length === 0 && (
          <p className="home-section-empty">
            {config
              ? "No agents configured yet."
              : "Agents will appear when configuration is available."}
          </p>
        )}
      </section>
    </div>
  );
}

export function Dashboard() {
  const [browse, setBrowse] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const switchView = (next: boolean) => {
    setBrowse(next);
    requestAnimationFrame(() =>
      (next ? backRef.current : headingRef.current)?.focus(),
    );
  };
  return browse ? (
    <div className="home-directory">
      <Button
        ref={backRef}
        variant="ghost"
        size="sm"
        className="mb-4"
        onClick={() => switchView(false)}
      >
        <ArrowLeft aria-hidden="true" className="mr-2 h-4 w-4" />
        Back to home
      </Button>
      <WorkspaceDirectory />
    </div>
  ) : (
    <Home onBrowse={() => switchView(true)} headingRef={headingRef} />
  );
}
