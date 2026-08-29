{{- define "backstage-portal.name" -}}
backstage-portal
{{- end -}}

{{- define "backstage-portal.fullname" -}}
{{- printf "%s" .Release.Name -}}
{{- end -}}

{{- define "backstage-portal.labels" -}}
app.kubernetes.io/name: {{ include "backstage-portal.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "backstage-portal.selectorLabels" -}}
app.kubernetes.io/name: {{ include "backstage-portal.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "backstage-portal.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "backstage-portal.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{/* In-cluster DNS name of the Service. */}}
{{- define "backstage-portal.serviceHost" -}}
{{- printf "%s.%s.svc.cluster.local" (include "backstage-portal.fullname" .) .Release.Namespace -}}
{{- end -}}
