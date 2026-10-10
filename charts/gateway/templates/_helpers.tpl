{{- define "gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "gateway.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Labels that identify this release's pods. The store and mcp-server charts allow
traffic from pods labelled app.kubernetes.io/name=gateway and
app.kubernetes.io/component=api, so these two must not change casually.
*/}}
{{- define "gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "gateway.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "gateway.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "gateway.fullname" .)) }}
{{- end }}

{{- define "gateway.url" -}}
{{- printf "%s://%s" (ternary "https" "http" .Values.ingress.tls.enabled) .Values.host }}
{{- end }}

{{- define "gateway.image" -}}
{{- $repository := ternary (printf "%s/%s" .registry .repository) .repository (not (empty .registry)) }}
{{- printf "%s:%s" $repository .tag }}
{{- end }}

{{/* Names by which pods inside the cluster may address the API's MCP endpoint. */}}
{{- define "gateway.api.allowedHosts" -}}
{{- $name := printf "%s-api" (include "gateway.fullname" .) }}
{{- $namespace := .Release.Namespace }}
{{- list $name (printf "%s.%s" $name $namespace) (printf "%s.%s.svc" $name $namespace) (printf "%s.%s.svc.%s" $name $namespace .Values.clusterDomain) | join "," }}
{{- end }}

{{- define "gateway.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "gateway.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end }}
