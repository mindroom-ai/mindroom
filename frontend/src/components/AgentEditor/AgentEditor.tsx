import {
  Fragment,
  useEffect,
  useState,
  useMemo,
  type ComponentProps,
} from "react";
import { useConfigStore } from "@/store/configStore";
import { useSwipeBack } from "@/hooks/useSwipeBack";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Plus, X, Bot } from "lucide-react";
import {
  EditorPanel,
  EditorPanelEmptyState,
  FieldGroup,
  CheckboxListField,
  CheckboxListItem,
  HistoryContextSection,
} from "@/components/shared";
import {
  Agent,
  AgentPrivateConfig,
  AgentPrivateKnowledgeConfig,
  MemoryBackend,
  ThreadMode,
  getDefaultPrivateConfig,
  resolveEffectiveDefaultTools,
  SHARED_CONTEXT_FILE_PLACEHOLDER,
} from "@/types/config";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useTools, type ToolInfo } from "@/hooks/useTools";
import { useSkills } from "@/hooks/useSkills";
import { useScopedConfigValidation } from "@/hooks/useScopedConfigValidation";
import { ToolConfigPanel } from "@/components/ToolConfig/ToolConfigPanel";
import { ToolSelectionRow } from "./ToolSelectionRow";
import { SchemaSection } from "@/components/SchemaForm";
import { setObjectKey } from "@/lib/configSchema";
import { toolEntriesForNames, toolNames } from "@/lib/toolEntry";

/** AgentConfig keys this editor renders by hand; More settings shows the rest. */
const AGENT_EDITOR_FIELDS = [
  "display_name",
  "role",
  "tools",
  "include_default_tools",
  "skills",
  "instructions",
  "rooms",
  "markdown",
  "model",
  "memory_backend",
  "compaction",
  "private",
  "knowledge_bases",
  "context_files",
  "thread_mode",
  "num_history_runs",
  "num_history_messages",
  "max_tool_calls_from_history",
  "show_tool_calls",
  "worker_tools",
  "allow_self_config",
  "delegate_to",
] as const;

// Private agents derive their execution scope from private.per.
const PRIVATE_AGENT_EDITOR_FIELDS = [...AGENT_EDITOR_FIELDS, "worker_scope"];

const PRIVATE_KNOWLEDGE_EDITOR_FIELDS = [
  "enabled",
  "description",
  "path",
  "watch",
] as const;

const TOOL_VALIDATION_UNAVAILABLE_MESSAGE =
  "Tool availability preview is unavailable while agent policy preview is unavailable. Save or refresh to validate tool assignments.";

/** Whether a checked tool's row opens settings: fields, overrides, or lazy loading. */
function toolHasSettings(tool: ToolInfo, hasOverrides: boolean): boolean {
  return (
    hasOverrides ||
    tool.lazy_loading_supported === true ||
    (tool.agent_override_fields?.length ?? 0) > 0 ||
    (tool.config_fields?.length ?? 0) > 0
  );
}

const blankToUndefined = (value: string) =>
  value.trim() === "" ? undefined : value;

const byDisplayName = (
  a: { display_name: string },
  b: { display_name: string },
) => a.display_name.localeCompare(b.display_name);

export function AgentEditor() {
  const {
    agents,
    rooms,
    selectedAgentId,
    updateAgent,
    setAgentPrivateEnabled,
    deleteAgent,
    saveConfig,
    config,
    agentPoliciesByAgent,
    isDirty,
    isLoading,
    selectAgent,
    getAgentToolOverrides,
  } = useConfigStore();

  const [activeToolName, setActiveToolName] = useState<string | null>(null);
  const selectedAgent = agents.find((a) => a.id === selectedAgentId);
  const defaultShowToolCalls = config?.defaults?.show_tool_calls ?? true;
  const defaultMarkdown = config?.defaults?.markdown ?? true;
  const globalMemoryBackend = config?.memory?.backend ?? "mem0";
  const knowledgeBaseNames = useMemo(
    () => Object.keys(config?.knowledge_bases || {}).sort(),
    [config?.knowledge_bases],
  );

  // Fetch tools and skills from backend
  const selectedAgentPolicy = selectedAgent
    ? (agentPoliciesByAgent[selectedAgent.id] ?? null)
    : null;
  const selectedExecutionScope =
    selectedAgentPolicy?.effective_execution_scope ?? null;
  const policyPreviewAvailable =
    selectedAgent == null || selectedAgentPolicy != null;
  const {
    tools: backendTools,
    loading: toolsLoading,
    statusAuthoritative,
  } = useTools(
    policyPreviewAvailable ? selectedAgentId : null,
    policyPreviewAvailable ? selectedExecutionScope : undefined,
  );
  const { skills: availableSkills, loading: skillsLoading } = useSkills();

  // Enable swipe back on mobile
  useSwipeBack({
    onSwipeBack: () => selectAgent(null),
    enabled: !!selectedAgentId && window.innerWidth < 1024, // Only on mobile when agent is selected
  });

  const selectedToolNames = useMemo(
    () => toolNames(selectedAgent?.tools ?? []),
    [selectedAgent?.tools],
  );
  const validationPrefix = useMemo<Array<string | number> | null>(
    () => (selectedAgentId == null ? null : ["agents", selectedAgentId]),
    [selectedAgentId],
  );
  const validationErrorForPath = useScopedConfigValidation(validationPrefix);
  const agentRootError = validationErrorForPath([], true);
  const numHistoryRunsError = validationErrorForPath(
    ["num_history_runs"],
    true,
  );
  const numHistoryMessagesError = validationErrorForPath(
    ["num_history_messages"],
    true,
  );
  const maxToolCallsFromHistoryError = validationErrorForPath(
    ["max_tool_calls_from_history"],
    true,
  );
  const compactionError = validationErrorForPath(["compaction"], true);
  const privateScopeError = validationErrorForPath(["private", "per"], true);
  const privateRootError = validationErrorForPath(["private", "root"], true);
  const privateTemplateDirError = validationErrorForPath(
    ["private", "template_dir"],
    true,
  );
  const privateContextFilesError = validationErrorForPath([
    "private",
    "context_files",
  ]);
  const privateKnowledgeError = validationErrorForPath(
    ["private", "knowledge"],
    true,
  );
  const privateKnowledgePathError = validationErrorForPath(
    ["private", "knowledge", "path"],
    true,
  );
  // Split tools into configured, default, and setup-required categories.
  const {
    configuredTools,
    defaultTools,
    setupRequiredTools,
    selectedUnavailableTools,
  } = useMemo(() => {
    const configured: ToolInfo[] = [];
    const defaults: ToolInfo[] = [];
    const setupRequired: ToolInfo[] = [];
    const selectedUnavailable: Array<{
      name: string;
      display_name: string;
      reason: string;
    }> = [];
    const backendToolNames = new Set<string>();

    backendTools.forEach((tool) => {
      backendToolNames.add(tool.name);
      // delegate is managed via delegate_to, not the tools picker
      if (tool.name === "delegate") return;
      if (tool.execution_scope_supported === false) {
        if (selectedToolNames.includes(tool.name)) {
          selectedUnavailable.push({
            name: tool.name,
            display_name: tool.display_name,
            reason:
              "Not supported for this execution scope. Uncheck it to remove it.",
          });
        }
        return;
      }
      // Tools that do not require configuration are default tools.
      if (tool.setup_type === "none") {
        defaults.push(tool);
      } else if (tool.status === "available") {
        configured.push(tool);
      } else {
        setupRequired.push(tool);
      }
    });

    for (const toolName of selectedToolNames) {
      if (toolName === "delegate" || backendToolNames.has(toolName)) {
        continue;
      }
      selectedUnavailable.push({
        name: toolName,
        display_name: toolName,
        reason:
          "This tool is no longer available in the current registry. Uncheck it to remove it.",
      });
    }

    return {
      configuredTools: configured.sort(byDisplayName),
      defaultTools: defaults.sort(byDisplayName),
      setupRequiredTools: setupRequired.sort(byDisplayName),
      selectedUnavailableTools: selectedUnavailable.sort(byDisplayName),
    };
  }, [selectedToolNames, backendTools]);
  // Compute effective tools: agent tools + defaults.tools (when include_default_tools is enabled)
  const effectiveTools = useMemo(() => {
    const tools = new Set(selectedToolNames);
    if (selectedAgent?.include_default_tools ?? true) {
      for (const t of resolveEffectiveDefaultTools(config?.defaults)) {
        tools.add(t);
      }
    }
    return [...tools];
  }, [
    selectedToolNames,
    selectedAgent?.include_default_tools,
    config?.defaults,
  ]);

  // Prepare checkbox items for skills (includes orphaned selected skills)
  const skillItems: CheckboxListItem[] = useMemo(() => {
    const selected = selectedAgent?.skills ?? [];
    const availableByName = new Map(availableSkills.map((s) => [s.name, s]));
    const allNames = [
      ...availableSkills.map((s) => s.name),
      ...selected.filter((name) => !availableByName.has(name)),
    ];
    return allNames.map((name) => ({
      value: name,
      label: name,
      description:
        availableByName.get(name)?.description ||
        "Skill not available; uncheck to remove",
    }));
  }, [availableSkills, selectedAgent?.skills]);

  // Prepare checkbox items for rooms
  const roomItems: CheckboxListItem[] = useMemo(
    () =>
      rooms.map((room) => ({
        value: room.id,
        label: room.display_name,
        description: room.description,
      })),
    [rooms],
  );
  const knowledgeBaseItems: CheckboxListItem[] = useMemo(
    () =>
      knowledgeBaseNames.map((baseName) => ({
        value: baseName,
        label: baseName,
      })),
    [knowledgeBaseNames],
  );

  // Include this agent so fresh self subagents can be explicitly allowed.
  const delegateItems: CheckboxListItem[] = useMemo(
    () =>
      agents.map((a) => ({
        value: a.id,
        label:
          a.id === selectedAgentId
            ? `${a.display_name} (this agent)`
            : a.display_name,
        description: a.role,
      })),
    [agents, selectedAgentId],
  );

  useEffect(() => {
    setActiveToolName((current) =>
      current != null && selectedToolNames.includes(current) ? current : null,
    );
  }, [selectedToolNames]);

  if (!selectedAgent) {
    return (
      <EditorPanelEmptyState icon={Bot} message="Select an agent to edit" />
    );
  }

  const privateConfig = selectedAgent.private;
  const privateKnowledge = privateConfig?.knowledge;

  const handleFieldChange = <K extends keyof Agent>(
    field: K,
    value: Agent[K],
  ) => {
    updateAgent(selectedAgent.id, { [field]: value } as Pick<Agent, K>);
  };

  const updateSelectedTools = (toolName: string, checked: boolean) => {
    const nextTools = checked
      ? [...new Set([...selectedToolNames, toolName])]
      : selectedToolNames.filter((tool) => tool !== toolName);
    handleFieldChange(
      "tools",
      toolEntriesForNames(nextTools, selectedAgent.tools),
    );
    setActiveToolName((currentActiveToolName) => {
      if (checked) {
        return toolName;
      }
      if (
        currentActiveToolName != null &&
        nextTools.includes(currentActiveToolName)
      ) {
        return currentActiveToolName;
      }
      return null;
    });
  };

  const handleDelete = () => {
    if (confirm("Are you sure you want to delete this agent?")) {
      deleteAgent(selectedAgent.id);
    }
  };

  const handleAddInstruction = () => {
    handleFieldChange("instructions", [...selectedAgent.instructions, ""]);
  };

  const handleRemoveInstruction = (index: number) => {
    handleFieldChange(
      "instructions",
      selectedAgent.instructions.filter((_, i) => i !== index),
    );
  };

  const handleAddContextFile = () => {
    handleFieldChange("context_files", [
      ...(selectedAgent.context_files ?? []),
      "",
    ]);
  };

  const handleRemoveContextFile = (index: number) => {
    handleFieldChange(
      "context_files",
      (selectedAgent.context_files ?? []).filter((_, i) => i !== index),
    );
  };

  const updatePrivate = (patch: Partial<AgentPrivateConfig>) => {
    handleFieldChange("private", {
      ...getDefaultPrivateConfig(selectedAgent),
      ...patch,
    });
  };

  const handleAddPrivateContextFile = () => {
    updatePrivate({
      context_files: [...(privateConfig?.context_files ?? []), ""],
    });
  };

  const handleRemovePrivateContextFile = (index: number) => {
    updatePrivate({
      context_files: (privateConfig?.context_files ?? []).filter(
        (_, i) => i !== index,
      ),
    });
  };

  const handleEnablePrivateKnowledge = (enabled: boolean) => {
    updatePrivate({
      knowledge: enabled
        ? {
            ...privateKnowledge,
            enabled: true,
            watch: privateKnowledge?.watch ?? true,
          }
        : { ...privateKnowledge, enabled: false },
    });
  };

  const updatePrivateKnowledge = (
    patch: Partial<AgentPrivateKnowledgeConfig>,
  ) => {
    updatePrivate({
      knowledge: {
        ...(privateKnowledge ?? { enabled: true, watch: true }),
        ...patch,
      },
    });
  };

  const workerTools =
    selectedAgent.worker_tools ?? config?.defaults?.worker_tools ?? [];
  const workerToolsInherited =
    selectedAgent.worker_tools == null &&
    config?.defaults?.worker_tools != null;

  const toolHasOverrides = (toolName: string): boolean => {
    const overrides = getAgentToolOverrides(selectedAgent.id, toolName);
    return overrides != null && Object.keys(overrides).length > 0;
  };

  const renderToolRow = (
    tool: ToolInfo,
    sectionKind: ComponentProps<typeof ToolSelectionRow>["sectionKind"],
    idPrefix: string,
  ) => {
    const isChecked = selectedToolNames.includes(tool.name);
    const hasOverrides = toolHasOverrides(tool.name);
    const isActive = activeToolName === tool.name;
    const showSettings = toolHasSettings(tool, hasOverrides);

    return (
      <Fragment key={tool.name}>
        <ToolSelectionRow
          tool={tool}
          sectionKind={sectionKind}
          checkboxId={`${idPrefix}-${tool.name}`}
          isChecked={isChecked}
          isActive={isActive}
          hasOverrides={hasOverrides}
          showSettings={showSettings}
          setupBlocked={
            sectionKind === "setupRequired" &&
            tool.dashboard_configuration_supported === false
          }
          onCheckedChange={(checked) => updateSelectedTools(tool.name, checked)}
          onToggleSettings={() =>
            setActiveToolName((prev) => (prev === tool.name ? null : tool.name))
          }
        />
        {isChecked && isActive && showSettings && (
          <ToolConfigPanel
            target={{
              kind: "agent",
              agentId: selectedAgent.id,
              lazyLoading: tool.lazy_loading_supported === true,
            }}
            toolName={tool.name}
            toolDisplayName={tool.display_name}
            overrideFields={tool.agent_override_fields ?? null}
            configFields={tool.config_fields ?? null}
          />
        )}
      </Fragment>
    );
  };

  return (
    <EditorPanel
      icon={Bot}
      title="Agent Details"
      isDirty={isDirty}
      isBusy={toolsLoading || skillsLoading}
      onSave={saveConfig}
      onDelete={handleDelete}
      disableSave={isLoading}
      onBack={() => selectAgent(null)}
    >
      {agentRootError && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {agentRootError}
        </div>
      )}

      {/* Display Name */}
      <FieldGroup
        label="Display Name"
        helperText="Human-readable name for the agent"
        htmlFor="display_name"
      >
        <Input
          id="display_name"
          value={selectedAgent.display_name}
          placeholder="Agent display name"
          onChange={(e) => handleFieldChange("display_name", e.target.value)}
        />
      </FieldGroup>

      {/* Role */}
      <FieldGroup
        label="Role Description"
        helperText="Description of the agent's purpose and capabilities"
        htmlFor="role"
      >
        <Textarea
          id="role"
          value={selectedAgent.role}
          placeholder="What this agent does..."
          rows={2}
          onChange={(e) => handleFieldChange("role", e.target.value)}
        />
      </FieldGroup>

      {/* Model Selection */}
      <FieldGroup
        label="Model"
        helperText="AI model to use (defaults to 'default' model if not specified)"
        htmlFor="model"
      >
        <Select
          value={selectedAgent.model || "default"}
          onValueChange={(value) => handleFieldChange("model", value)}
        >
          <SelectTrigger id="model">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {config &&
              Object.keys(config.models).map((modelId) => (
                <SelectItem key={modelId} value={modelId}>
                  {modelId}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Memory Backend */}
      <FieldGroup
        label="Memory Backend"
        helperText={`Inherit global backend (${globalMemoryBackend}) or override for this agent.`}
        htmlFor="memory_backend"
      >
        <Select
          value={selectedAgent.memory_backend ?? "inherit"}
          onValueChange={(value) =>
            handleFieldChange(
              "memory_backend",
              value === "inherit" ? undefined : (value as MemoryBackend),
            )
          }
        >
          <SelectTrigger id="memory_backend">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="inherit">
              Inherit global ({globalMemoryBackend})
            </SelectItem>
            <SelectItem value="mem0">Mem0 (vector memory)</SelectItem>
            <SelectItem value="file">File (markdown memory)</SelectItem>
            <SelectItem value="none">Disabled (stateless)</SelectItem>
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Thread Mode */}
      <FieldGroup
        label="Thread Mode"
        helperText="'thread' creates Matrix threads per conversation; 'room' uses a single continuous conversation per room (ideal for bridges/mobile)"
        htmlFor="thread_mode"
      >
        <Select
          value={selectedAgent.thread_mode ?? "thread"}
          onValueChange={(value) =>
            handleFieldChange("thread_mode", value as ThreadMode)
          }
        >
          <SelectTrigger id="thread_mode">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="thread">Thread (default)</SelectItem>
            <SelectItem value="room">Room (continuous)</SelectItem>
          </SelectContent>
        </Select>
      </FieldGroup>

      {/* Knowledge Bases */}
      <FieldGroup
        label="Knowledge Bases"
        helperText="Assign one or more knowledge bases for this agent to search"
      >
        <CheckboxListField
          items={knowledgeBaseItems}
          selected={selectedAgent.knowledge_bases ?? []}
          onChange={(next) => handleFieldChange("knowledge_bases", next)}
          idPrefix="knowledge-base"
          emptyMessage="No knowledge bases available. Add one in the Knowledge tab."
          className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2"
        />
      </FieldGroup>

      {/* Allowed subagents */}
      <FieldGroup
        label="Allowed subagents"
        helperText="Run these agents with fresh context and wait for their results. Select this agent to allow fresh copies of itself."
      >
        <CheckboxListField
          items={delegateItems}
          selected={selectedAgent.delegate_to ?? []}
          onChange={(next) => handleFieldChange("delegate_to", next)}
          idPrefix="delegate"
          emptyMessage="No agents available."
          className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2"
        />
      </FieldGroup>

      {/* Context Files */}
      <FieldGroup
        label="Context Files"
        helperText={
          privateConfig != null
            ? "Shared workspace-relative files loaded into each agent instance. Use Private Context Files below for requester-local files."
            : "Workspace-relative files loaded into each freshly built agent instance and prepended to its role context."
        }
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={handleAddContextFile}
            className="h-9 px-3"
            data-testid="add-context-file-button"
          >
            <Plus className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Add</span>
          </Button>
        }
      >
        <div className="space-y-2">
          {(selectedAgent.context_files ?? []).map((filePath, index) => (
            <div key={index} className="flex gap-2">
              <Input
                value={filePath}
                onChange={(e) => {
                  const updated = [...(selectedAgent.context_files ?? [])];
                  updated[index] = e.target.value;
                  handleFieldChange("context_files", updated);
                }}
                placeholder={SHARED_CONTEXT_FILE_PLACEHOLDER}
                className="min-h-[40px]"
              />
              <Button
                variant="ghost"
                size="icon"
                onClick={() => handleRemoveContextFile(index)}
                className="h-10 w-10 flex-shrink-0"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      </FieldGroup>

      <div className="border-t border-gray-200 dark:border-gray-700 pt-4 mt-2">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-4">
          Private Instance
        </h3>

        <FieldGroup
          label="Requester-Private State"
          helperText="Enable requester-local state for one shared agent definition. Private agents cannot join configured teams or transitive delegation, but explicitly tagged ad hoc teams can include them for that requester."
          htmlFor="private_enabled"
        >
          <div className="flex items-center gap-2">
            <Checkbox
              id="private_enabled"
              checked={privateConfig != null}
              onCheckedChange={(checked) =>
                setAgentPrivateEnabled(selectedAgent.id, checked === true)
              }
            />
            <label
              htmlFor="private_enabled"
              className="text-sm font-medium cursor-pointer select-none"
            >
              Enable requester-private state
            </label>
          </div>
        </FieldGroup>

        {privateConfig != null && (
          <>
            <FieldGroup
              label="Private Scope"
              helperText="Requester boundary that gets its own private instance. Private agents derive their execution scope from this boundary."
              htmlFor="private_per"
              error={privateScopeError}
            >
              <Select
                value={privateConfig.per}
                onValueChange={(value) =>
                  updatePrivate({ per: value as AgentPrivateConfig["per"] })
                }
              >
                <SelectTrigger id="private_per">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="user">user</SelectItem>
                  <SelectItem value="user_agent">user_agent</SelectItem>
                </SelectContent>
              </Select>
            </FieldGroup>

            <FieldGroup
              label="Private Root"
              helperText="Optional requester-local root name under the canonical private-instance state root."
              htmlFor="private_root"
              error={privateRootError}
            >
              <Input
                id="private_root"
                value={privateConfig.root ?? ""}
                placeholder="mind_data"
                onChange={(e) =>
                  updatePrivate({
                    root: blankToUndefined(e.target.value),
                  })
                }
              />
            </FieldGroup>

            <FieldGroup
              label="Template Directory"
              helperText="Optional local directory copied into each requester root without overwriting existing files."
              htmlFor="private_template_dir"
              error={privateTemplateDirError}
            >
              <Input
                id="private_template_dir"
                value={privateConfig.template_dir ?? ""}
                placeholder="./mind_template"
                onChange={(e) =>
                  updatePrivate({
                    template_dir: blankToUndefined(e.target.value),
                  })
                }
              />
            </FieldGroup>

            <FieldGroup
              label="Private Context Files"
              helperText="Private-root-relative files loaded into role context for each requester-private instance."
              error={privateContextFilesError}
              actions={
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleAddPrivateContextFile}
                  className="h-9 px-3"
                >
                  <Plus className="h-4 w-4 sm:mr-1" />
                  <span className="hidden sm:inline">Add</span>
                </Button>
              }
            >
              <div className="space-y-2">
                {(privateConfig.context_files ?? []).map((filePath, index) => (
                  <div key={index} className="flex gap-2">
                    <Input
                      value={filePath}
                      onChange={(e) => {
                        const updated = [
                          ...(privateConfig.context_files ?? []),
                        ];
                        updated[index] = e.target.value;
                        updatePrivate({ context_files: updated });
                      }}
                      placeholder="SOUL.md"
                      className="min-h-[40px]"
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => handleRemovePrivateContextFile(index)}
                      className="h-10 w-10 flex-shrink-0"
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                ))}
              </div>
            </FieldGroup>

            <FieldGroup
              label="Private Knowledge"
              helperText="Requester-local knowledge indexed from inside the private root."
              htmlFor="private_knowledge_enabled"
              error={privateKnowledgeError}
            >
              <div className="flex items-center gap-2">
                <Checkbox
                  id="private_knowledge_enabled"
                  checked={privateKnowledge?.enabled ?? false}
                  onCheckedChange={(checked) =>
                    handleEnablePrivateKnowledge(checked === true)
                  }
                />
                <label
                  htmlFor="private_knowledge_enabled"
                  className="text-sm font-medium cursor-pointer select-none"
                >
                  Enable private knowledge
                </label>
              </div>
            </FieldGroup>

            {privateKnowledge?.enabled === true && (
              <>
                <FieldGroup
                  label="Private Knowledge Description"
                  helperText="Shown in the knowledge search tool metadata for this requester-private source."
                  htmlFor="private_knowledge_description"
                >
                  <Textarea
                    id="private_knowledge_description"
                    value={privateKnowledge.description ?? ""}
                    placeholder="Requester-private notes, preferences, and working memory"
                    onChange={(e) =>
                      updatePrivateKnowledge({ description: e.target.value })
                    }
                  />
                </FieldGroup>

                <FieldGroup
                  label="Private Knowledge Path"
                  helperText="Private-root-relative path to index as private agent knowledge."
                  htmlFor="private_knowledge_path"
                  error={privateKnowledgePathError}
                >
                  <Input
                    id="private_knowledge_path"
                    value={privateKnowledge.path ?? ""}
                    placeholder="memory"
                    onChange={(e) =>
                      updatePrivateKnowledge({
                        path: blankToUndefined(e.target.value),
                      })
                    }
                  />
                </FieldGroup>

                <FieldGroup
                  label="Refresh Private Knowledge"
                  helperText="Schedule refresh on access for private agent knowledge."
                  htmlFor="private_knowledge_watch"
                >
                  <div className="flex items-center gap-2">
                    <Checkbox
                      id="private_knowledge_watch"
                      checked={privateKnowledge.watch ?? true}
                      onCheckedChange={(checked) =>
                        updatePrivateKnowledge({ watch: checked === true })
                      }
                    />
                    <label
                      htmlFor="private_knowledge_watch"
                      className="text-sm font-medium cursor-pointer select-none"
                    >
                      Refresh on access
                    </label>
                  </div>
                </FieldGroup>

                <SchemaSection
                  title="More private knowledge settings"
                  definition="AgentPrivateKnowledgeConfig"
                  value={privateKnowledge}
                  path={["agents", selectedAgent.id, "private", "knowledge"]}
                  exclude={PRIVATE_KNOWLEDGE_EDITOR_FIELDS}
                  onFieldChange={(key, next) =>
                    updatePrivate({
                      knowledge: setObjectKey(privateKnowledge, key, next),
                    })
                  }
                />
              </>
            )}
          </>
        )}
      </div>

      {/* Include Default Tools */}
      <FieldGroup
        label="Include Default Tools"
        helperText="Whether to merge the global default tools into this agent's tools"
        htmlFor="include_default_tools"
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id="include_default_tools"
            checked={selectedAgent.include_default_tools ?? true}
            onCheckedChange={(checked) =>
              handleFieldChange("include_default_tools", checked === true)
            }
          />
          <label
            htmlFor="include_default_tools"
            className="text-sm font-medium cursor-pointer select-none"
          >
            Include default tools
          </label>
        </div>
      </FieldGroup>

      {/* Tools */}
      <FieldGroup label="Tools" helperText="Select tools this agent can use">
        <div className="space-y-4">
          {!policyPreviewAvailable && (
            <Alert>
              <AlertDescription>
                Agent policy preview is unavailable. Save or refresh to
                re-validate tool scope support for this draft.
              </AlertDescription>
            </Alert>
          )}
          {selectedExecutionScope != null && statusAuthoritative === false && (
            <Alert>
              <AlertDescription>
                Requester-scoped tool status is preview only. The dashboard can
                show scope support rules and shared env-backed availability, but
                it cannot inspect live requester-owned scoped credentials.
              </AlertDescription>
            </Alert>
          )}
          {!policyPreviewAvailable ? (
            <div className="text-sm text-muted-foreground text-center py-4">
              {TOOL_VALIDATION_UNAVAILABLE_MESSAGE}
            </div>
          ) : toolsLoading ? (
            <div className="text-sm text-muted-foreground text-center py-4">
              Loading available tools...
            </div>
          ) : configuredTools.length === 0 &&
            defaultTools.length === 0 &&
            setupRequiredTools.length === 0 &&
            selectedUnavailableTools.length === 0 ? (
            <div className="text-sm text-muted-foreground text-center py-4">
              No tools are available. Please configure tools in the Tools tab
              first.
            </div>
          ) : (
            <>
              {selectedUnavailableTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Selected But Unavailable
                    </h4>
                    <Badge variant="destructive" className="text-xs">
                      {selectedUnavailableTools.length}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      (uncheck to remove invalid tool assignments)
                    </span>
                  </div>
                  <div className="pl-2 space-y-1">
                    {selectedUnavailableTools.map((tool) => (
                      <div
                        key={tool.name}
                        className="rounded-lg border border-destructive/30 bg-destructive/5 p-2"
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center space-x-3 sm:space-x-2">
                            <Checkbox
                              id={`unavailable-${tool.name}`}
                              checked
                              onCheckedChange={(checked) => {
                                updateSelectedTools(
                                  tool.name,
                                  checked === true,
                                );
                              }}
                              className="h-5 w-5 sm:h-4 sm:w-4"
                            />
                            <label
                              htmlFor={`unavailable-${tool.name}`}
                              className="text-sm font-medium leading-none cursor-pointer select-none"
                            >
                              {tool.display_name}
                            </label>
                          </div>
                          <Badge variant="destructive" className="text-xs">
                            Remove
                          </Badge>
                        </div>
                        <p className="pl-8 pt-1 text-xs text-muted-foreground">
                          {tool.reason}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {selectedUnavailableTools.length > 0 &&
                (configuredTools.length > 0 ||
                  defaultTools.length > 0 ||
                  setupRequiredTools.length > 0) && (
                  <div className="border-t border-gray-200 dark:border-gray-700" />
                )}

              {/* Configured Tools Section */}
              {configuredTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Configured Tools
                    </h4>
                    <Badge
                      variant="default"
                      className="text-xs bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                    >
                      {configuredTools.length}
                    </Badge>
                  </div>
                  <div className="pl-2 space-y-1">
                    {configuredTools.map((tool) =>
                      renderToolRow(tool, "configured", "configured"),
                    )}
                  </div>
                </div>
              )}

              {/* Divider if both sections have content */}
              {configuredTools.length > 0 &&
                (defaultTools.length > 0 || setupRequiredTools.length > 0) && (
                  <div className="border-t border-gray-200 dark:border-gray-700" />
                )}

              {/* Default Tools Section */}
              {defaultTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Default Tools
                    </h4>
                    <Badge variant="secondary" className="text-xs">
                      {defaultTools.length}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      (work without configuration)
                    </span>
                  </div>
                  <div className="pl-2 space-y-1">
                    {defaultTools.map((tool) =>
                      renderToolRow(tool, "default", "default"),
                    )}
                  </div>
                </div>
              )}

              {/* Divider if a setup-required section follows */}
              {(configuredTools.length > 0 || defaultTools.length > 0) &&
                setupRequiredTools.length > 0 && (
                  <div className="border-t border-gray-200 dark:border-gray-700" />
                )}

              {/* Setup Required Tools Section */}
              {setupRequiredTools.length > 0 && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                      Setup Required
                    </h4>
                    <Badge variant="outline" className="text-xs">
                      {setupRequiredTools.length}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      (selectable after credentials are configured)
                    </span>
                  </div>
                  <div className="pl-2 space-y-1">
                    {setupRequiredTools.map((tool) =>
                      renderToolRow(tool, "setupRequired", "setup"),
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </FieldGroup>

      {/* Skills */}
      <FieldGroup
        label="Skills"
        helperText="Select skills this agent can invoke"
      >
        {skillsLoading ? (
          <div className="text-sm text-muted-foreground text-center py-2">
            Loading skills...
          </div>
        ) : (
          <CheckboxListField
            items={skillItems}
            selected={selectedAgent.skills}
            onChange={(next) => handleFieldChange("skills", next)}
            idPrefix="skill"
            emptyMessage="No skills available. Create skills in the Skills tab first."
          />
        )}
      </FieldGroup>

      {/* Instructions */}
      <FieldGroup
        label="Instructions"
        helperText="Custom instructions for this agent"
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={handleAddInstruction}
            data-testid="add-instruction-button"
            className="h-9 px-3"
          >
            <Plus className="h-4 w-4 sm:mr-1" />
            <span className="hidden sm:inline">Add</span>
          </Button>
        }
      >
        <div className="space-y-2">
          {selectedAgent.instructions.map((instruction, index) => (
            <div key={index} className="flex gap-2">
              <Input
                value={instruction}
                onChange={(e) => {
                  const updated = [...selectedAgent.instructions];
                  updated[index] = e.target.value;
                  handleFieldChange("instructions", updated);
                }}
                placeholder="Instruction..."
                className="min-h-[40px]"
              />
              <Button
                variant="ghost"
                size="icon"
                onClick={() => handleRemoveInstruction(index)}
                className="h-10 w-10 flex-shrink-0"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      </FieldGroup>

      {/* Rooms */}
      <FieldGroup
        label="Agent Rooms"
        helperText="Select rooms where this agent can operate"
      >
        <CheckboxListField
          items={roomItems}
          selected={selectedAgent.rooms}
          onChange={(next) => handleFieldChange("rooms", next)}
          idPrefix="room"
          emptyMessage="No rooms available. Create rooms in the Rooms tab."
          className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-2"
        />
      </FieldGroup>

      {/* Markdown */}
      <FieldGroup
        label="Markdown"
        helperText={`Tell the agent its replies are shown as Markdown (global default: ${
          defaultMarkdown ? "on" : "off"
        })`}
        htmlFor="markdown"
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id="markdown"
            checked={selectedAgent.markdown ?? defaultMarkdown}
            onCheckedChange={(checked) =>
              handleFieldChange("markdown", checked === true)
            }
          />
          <label
            htmlFor="markdown"
            className="text-sm font-medium cursor-pointer select-none"
          >
            Enable markdown
          </label>
        </div>
      </FieldGroup>

      {/* Show Tool Calls */}
      <FieldGroup
        label="Show Tool Calls"
        helperText="Display tool call details inline in agent responses"
        htmlFor="show_tool_calls"
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id="show_tool_calls"
            checked={selectedAgent.show_tool_calls ?? defaultShowToolCalls}
            onCheckedChange={(checked) =>
              handleFieldChange("show_tool_calls", checked === true)
            }
          />
          <label
            htmlFor="show_tool_calls"
            className="text-sm font-medium cursor-pointer select-none"
          >
            Show tool calls inline
          </label>
        </div>
      </FieldGroup>

      {/* Worker Tools */}
      {effectiveTools.length > 0 && (
        <FieldGroup
          label="Worker Tools"
          helperText={`Select which of this agent's tools to route through worker-scoped execution via the sandbox proxy${
            config?.defaults?.worker_tools != null
              ? ` (default: ${
                  config.defaults.worker_tools.length > 0
                    ? config.defaults.worker_tools.join(", ")
                    : "none"
                })`
              : ""
          }`}
        >
          <div className="space-y-1 max-h-48 overflow-y-auto border rounded-lg p-2">
            {effectiveTools.map((toolName) => {
              const isChecked = workerTools.includes(toolName);
              const toggle = () => {
                // On first interaction when inheriting, seed from defaults
                const updated = isChecked
                  ? workerTools.filter((t) => t !== toolName)
                  : [...workerTools, toolName];
                handleFieldChange("worker_tools", updated);
              };
              return (
                <div
                  key={toolName}
                  className="flex items-center space-x-3 sm:space-x-2 p-2 rounded-lg hover:bg-secondary transition-colors"
                >
                  <Checkbox
                    aria-label={`worker ${toolName}`}
                    checked={isChecked}
                    onCheckedChange={toggle}
                    className="h-5 w-5 sm:h-4 sm:w-4"
                  />
                  <span
                    role="none"
                    onClick={toggle}
                    className={`text-sm font-medium leading-none cursor-pointer select-none${
                      workerToolsInherited && isChecked
                        ? " text-gray-400 dark:text-gray-500 italic"
                        : ""
                    }`}
                  >
                    {toolName}
                    {workerToolsInherited && isChecked ? " (default)" : ""}
                  </span>
                </div>
              );
            })}
          </div>
        </FieldGroup>
      )}

      {/* Allow Self Config */}
      <FieldGroup
        label="Allow Self Config"
        helperText="Let this agent read and modify its own configuration at runtime"
        htmlFor="allow_self_config"
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id="allow_self_config"
            checked={
              selectedAgent.allow_self_config ??
              config?.defaults?.allow_self_config ??
              false
            }
            onCheckedChange={(checked) =>
              handleFieldChange("allow_self_config", checked === true)
            }
          />
          <label
            htmlFor="allow_self_config"
            className="text-sm font-medium cursor-pointer select-none"
          >
            Enable self-configuration
          </label>
        </div>
      </FieldGroup>

      <HistoryContextSection
        values={selectedAgent}
        onChange={(field, value) =>
          updateAgent(selectedAgent.id, { [field]: value })
        }
        resetKey={selectedAgentId}
        compactionPath={["agents", selectedAgent.id, "compaction"]}
        defaults={config?.defaults}
        historyRunsHelperText={`Number of prior conversation runs to include as history context. Leave empty to use default${
          config?.defaults?.num_history_runs != null
            ? ` (${config.defaults.num_history_runs})`
            : " (all)"
        }.`}
        historyMessagesHelperText={`Max messages from history (mutually exclusive with History Runs). Leave empty to use default${
          config?.defaults?.num_history_messages != null
            ? ` (${config.defaults.num_history_messages})`
            : " (all)"
        }.`}
        maxToolCallsHelperText={`Max tool call messages replayed from history. Leave empty to use default${
          config?.defaults?.max_tool_calls_from_history != null
            ? ` (${config.defaults.max_tool_calls_from_history})`
            : " (no limit)"
        }.`}
        autoCompactionHelperText="Automatically compact older session history before a run when the request crosses the compaction threshold on a route without native compaction, or when raw replay exceeds the hard context budget."
        thresholdTokensHelperText="Compaction trigger in tokens, counted over the whole request. Supported OpenAI and Claude routes compact natively at it; every other route runs text compaction before the reply."
        compactionModelPlaceholder={
          config?.defaults?.compaction?.model ?? "Default: agent model"
        }
        numHistoryRunsError={numHistoryRunsError}
        numHistoryMessagesError={numHistoryMessagesError}
        maxToolCallsFromHistoryError={maxToolCallsFromHistoryError}
        compactionError={compactionError}
      />

      <SchemaSection
        title="More settings"
        definition="AgentConfig"
        value={selectedAgent}
        path={["agents", selectedAgent.id]}
        exclude={
          selectedAgent.private != null
            ? PRIVATE_AGENT_EDITOR_FIELDS
            : AGENT_EDITOR_FIELDS
        }
        onFieldChange={(key, next) =>
          updateAgent(selectedAgent.id, { [key]: next } as Partial<Agent>)
        }
      />
    </EditorPanel>
  );
}
