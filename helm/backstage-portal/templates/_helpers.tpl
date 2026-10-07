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

{{/*
  Which database Backstage runs on, after refusing the combinations that look
  fine at install and fail later: replicas on sqlite (each pod its own signing
  keys — a sign-in on one is refused by the next), postgres with neither a Secret nor
  Cloud SQL (the pod would crash-loop on ${POSTGRES_HOST}), and a sqlite PVC under postgres.
*/}}
{{- define "backstage-portal.dbType" -}}
{{- $type := (.Values.database).type | default "sqlite" -}}
{{- if not (has $type (list "sqlite" "postgres")) -}}
{{- fail (printf "database.type must be sqlite or postgres, not %q" $type) -}}
{{- end -}}
{{- if and (eq $type "sqlite") (gt (int .Values.replicaCount) 1) -}}
{{- fail "replicaCount > 1 needs database.type: postgres — on sqlite each replica has its own sign-in keys" -}}
{{- end -}}
{{- if eq $type "postgres" -}}
{{- $pg := .Values.database.postgres -}}
{{- if $pg.cloudSql.enabled -}}
{{- if not (and $pg.cloudSql.instance $pg.cloudSql.user) -}}
{{- fail "database.postgres.cloudSql needs instance (project:region:instance) and user (the IAM database user)" -}}
{{- end -}}
{{- else if not $pg.existingSecret -}}
{{- fail "database.type: postgres needs database.postgres.existingSecret (POSTGRES_HOST/PORT/USER/PASSWORD), or cloudSql.enabled" -}}
{{- end -}}
{{- if .Values.persistence.enabled -}}
{{- fail "persistence is the sqlite volume — turn it off with database.type: postgres" -}}
{{- end -}}
{{- end -}}
{{- $type -}}
{{- end -}}
