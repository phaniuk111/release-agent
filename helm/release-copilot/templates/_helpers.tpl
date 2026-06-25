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
