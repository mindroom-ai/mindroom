import { useId, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AUTH_CHOICES, DEFAULT_TEMPLATE } from "./egressRuleAuth";
import {
  type RuleForm,
  applyAuthChoice,
  splitHostInput,
} from "./egressServiceForm";
import type { RuleFieldPart } from "./egressServiceValidation";
import type { EgressRuleAuthType } from "./types";

/**
 * The editor's labelled fields and the fields of one rule. The first rule is
 * edited in the basic form (`RuleHostField`, `RuleSendFields`) and under More
 * options (`RulePathAndPortFields`); `BASIC_RULE_PARTS` in
 * `egressServiceValidation.ts` names the parts the basic form shows. Every
 * other rule is an `ExtraRule`.
 */

export const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm aria-invalid:border-destructive";
export const inputClass = "h-9 aria-invalid:border-destructive";
export const errorClass = "text-xs text-destructive";
export const warningClass = "text-xs text-amber-700 dark:text-amber-400";

/** What a control needs to point at the hint and the error its field shows. */
export interface ControlProps {
  id: string;
  "aria-invalid"?: true;
  "aria-describedby"?: string;
}

export function Field({
  label,
  id,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  id: string;
  hint?: string;
  error?: string;
  children: (control: ControlProps) => ReactNode;
  className?: string;
}) {
  const describedBy = [hint ? `${id}-hint` : null, error ? `${id}-error` : null]
    .filter((part) => part !== null)
    .join(" ");
  return (
    <div className={`space-y-1 ${className ?? ""}`}>
      <label htmlFor={id} className="text-xs font-medium">
        {label}
      </label>
      {children({
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": describedBy || undefined,
      })}
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className={errorClass}>
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * How a rule is edited: the first rule in the basic fields, the others in full
 * under More options. The fields of an extra rule (index above 0) say which
 * rule they belong to.
 */
export interface RuleEditing {
  index: number;
  rule: RuleForm;
  onChange: (rule: RuleForm) => void;
  errorFor: (part: RuleFieldPart) => string | undefined;
  disabled: boolean;
}

export function RuleHostField({
  index,
  rule,
  onChange,
  errorFor,
  disabled,
}: RuleEditing) {
  const id = useId();
  const extra = index > 0;
  // The path a pasted address carried into the Path prefix, until the next change.
  const [moved, setMoved] = useState<string | null>(null);
  return (
    <Field
      id={`${id}-host`}
      label={extra ? "Host" : "Website"}
      hint="For example api.example.com, or *.example.com for every subdomain"
      error={errorFor("host")}
    >
      {(control) => (
        <>
          <Input
            {...control}
            aria-label={extra ? `Rule ${index + 1} host` : undefined}
            className={inputClass}
            value={rule.host}
            disabled={disabled}
            // A pasted address keeps its host, and its path goes to the Path prefix when that is empty.
            onChange={(event) => {
              const { host, path } = splitHostInput(event.target.value);
              const move =
                path !== "" && path !== "/" && rule.pathPrefix.trim() === "";
              setMoved(move ? path : null);
              onChange({
                ...rule,
                host,
                ...(move ? { pathPrefix: path } : {}),
              });
            }}
          />
          {moved !== null && (
            <p role="status" className="text-xs text-muted-foreground">
              Moved {moved} to Path prefix
            </p>
          )}
        </>
      )}
    </Field>
  );
}

export function RulePathAndPortFields({
  index,
  rule,
  onChange,
  errorFor,
  disabled,
}: RuleEditing) {
  const id = useId();
  const extra = index > 0;
  return (
    <>
      <Field
        id={`${id}-path`}
        label="Path prefix"
        error={errorFor("pathPrefix")}
      >
        {(control) => (
          <Input
            {...control}
            aria-label={extra ? `Rule ${index + 1} path prefix` : undefined}
            placeholder="/ (every path)"
            className={inputClass}
            value={rule.pathPrefix}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...rule, pathPrefix: event.target.value })
            }
          />
        )}
      </Field>
      <Field id={`${id}-port`} label="Port" error={errorFor("port")}>
        {(control) => (
          <Input
            {...control}
            aria-label={extra ? `Rule ${index + 1} port` : undefined}
            inputMode="numeric"
            placeholder="any"
            className={inputClass}
            value={rule.port}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...rule, port: event.target.value })
            }
          />
        )}
      </Field>
    </>
  );
}

/** How the rule sends the key, with the fields that way needs. */
export function RuleSendFields({
  index,
  rule,
  onChange,
  errorFor,
  disabled,
}: RuleEditing) {
  const id = useId();
  const named = ["header", "template", "query"].includes(rule.authType);
  const ariaLabel = (part: string) =>
    index > 0 ? `Rule ${index + 1} ${part}` : undefined;
  return (
    <>
      <Field id={`${id}-auth`} label="Send the key as">
        {(control) => (
          <select
            {...control}
            aria-label={ariaLabel("auth type")}
            className={selectClass}
            value={rule.authType}
            disabled={disabled}
            onChange={(event) =>
              onChange(
                applyAuthChoice(rule, event.target.value as EgressRuleAuthType),
              )
            }
          >
            {AUTH_CHOICES.map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      {rule.authType === "basic" && (
        <Field
          id={`${id}-username`}
          label="Username"
          error={errorFor("username")}
        >
          {(control) => (
            <Input
              {...control}
              aria-label={ariaLabel("username")}
              className={inputClass}
              value={rule.username}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...rule, username: event.target.value })
              }
            />
          )}
        </Field>
      )}
      {named && (
        <Field
          id={`${id}-name`}
          label={rule.authType === "query" ? "Parameter name" : "Header name"}
          hint={
            rule.authType === "query"
              ? "For example api_key"
              : "For example X-Api-Key"
          }
          error={errorFor("authName")}
        >
          {(control) => (
            <Input
              {...control}
              aria-label={ariaLabel(
                rule.authType === "query" ? "parameter name" : "header name",
              )}
              className={inputClass}
              value={rule.headerOrParam}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...rule, headerOrParam: event.target.value })
              }
            />
          )}
        </Field>
      )}
      {(rule.authType === "template" || rule.authType === "query") && (
        <Field
          id={`${id}-template`}
          label="Value template"
          hint="Leave empty to send the key as is."
          error={errorFor("template")}
        >
          {(control) => (
            <Input
              {...control}
              aria-label={ariaLabel("template")}
              placeholder={DEFAULT_TEMPLATE}
              className={inputClass}
              value={rule.template}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...rule, template: event.target.value })
              }
            />
          )}
        </Field>
      )}
    </>
  );
}

/** A rule beyond the first, with all its fields; a limit's rule sends no key, so it has no way to send one. */
export function ExtraRule(
  props: RuleEditing & { onRemove: () => void; sendsKey?: boolean },
) {
  const { onRemove, sendsKey = true, ...editing } = props;
  const label = `Rule ${editing.index + 1}`;
  return (
    <li
      aria-label={label}
      className="space-y-3 rounded-md border border-border/60 p-3"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium">{label}</span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          aria-label={`Remove ${label}`}
          disabled={editing.disabled}
          onClick={onRemove}
        >
          Remove
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <RuleHostField {...editing} />
        <RulePathAndPortFields {...editing} />
      </div>
      {sendsKey && (
        <div className="grid gap-3 sm:grid-cols-3">
          <RuleSendFields {...editing} />
        </div>
      )}
    </li>
  );
}
