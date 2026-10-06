{{/* Expand the name of the chart. */}}
{{- define "release-copilot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "release-copilot.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "release-copilot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "release-copilot.labels" -}}
helm.sh/chart: {{ include "release-copilot.chart" . }}
{{ include "release-copilot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "release-copilot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "release-copilot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* ServiceAccount name to use. */}}
{{- define "release-copilot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "release-copilot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret holding the GitHub token. */}}
{{- define "release-copilot.secretName" -}}
{{- if .Values.githubToken.existingSecret -}}
{{- .Values.githubToken.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "release-copilot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* In-cluster DNS name of the Service. */}}
{{- define "release-copilot.serviceHost" -}}
{{- printf "%s.%s.svc.cluster.local" (include "release-copilot.fullname" .) .Release.Namespace -}}
{{- end -}}

{{/*
supportTable.columns → the SUPPORT_COLUMNS string ("role=column[:Label],…").
A column is a name, or {column, label}. A comma or an equals sign in a name or
a label would split the string in the wrong place, so both are refused here,
at render time, with the role named.
*/}}
{{- define "release-copilot.supportColumns" -}}
{{- $parts := list -}}
{{- range $role, $c := (required "supportTable.columns is required when supportTable.table is set" .columns) -}}
{{- $col := "" -}}{{- $label := "" -}}
{{- if kindIs "string" $c -}}{{- $col = $c -}}
{{- else -}}{{- $col = required (printf "supportTable.columns.%s.column is required" $role) $c.column -}}{{- $label = $c.label | default "" -}}
{{- end -}}
{{- if or (contains "," $col) (contains "=" $col) (contains ":" $col) (contains "," $label) (contains "=" $label) -}}
{{- fail (printf "supportTable.columns.%s: a column or label may not contain ',' '=' or ':'" $role) -}}
{{- end -}}
{{- if $label -}}{{- $parts = append $parts (printf "%s=%s:%s" $role $col $label) -}}
{{- else -}}{{- $parts = append $parts (printf "%s=%s" $role $col) -}}
{{- end -}}
{{- end -}}
{{- join "," $parts -}}
{{- end -}}

{{/*
supportTable.owners → the SUPPORT_OWNERS string ("role:value=Team,…,default=Team").
owners: { default: Team, system: { <value>: Team }, process: { <value>: Team } }
*/}}
{{- define "release-copilot.supportOwners" -}}
{{- $parts := list -}}
{{- range $role, $m := .owners -}}
{{- if ne $role "default" -}}
{{- range $value, $team := $m -}}
{{- if or (contains "," $value) (contains "=" $value) (contains "," $team) (contains "=" $team) -}}
{{- fail (printf "supportTable.owners.%s: a value or team may not contain ',' or '='" $role) -}}
{{- end -}}
{{- $parts = append $parts (printf "%s:%s=%s" $role $value $team) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- with .owners.default -}}{{- $parts = append $parts (printf "default=%s" .) -}}{{- end -}}
{{- join "," $parts -}}
{{- end -}}
