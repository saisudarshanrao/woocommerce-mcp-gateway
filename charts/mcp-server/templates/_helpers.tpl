{{- define "mcp-server.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-mcp" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "mcp-server.selectorLabels" -}}
app.kubernetes.io/name: mcp-server
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-server.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "mcp-server.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "mcp-server.secretName" -}}
{{- .Values.auth.existingSecret | default (printf "%s-auth" (include "mcp-server.fullname" .)) }}
{{- end }}

{{- define "mcp-server.image" -}}
{{- $image := .Values.image }}
{{- if $image.reference }}
{{- $image.reference }}
{{- else }}
{{- $repository := ternary (printf "%s/%s" $image.registry $image.repository) $image.repository (not (empty $image.registry)) }}
{{- printf "%s:%s" $repository $image.tag }}
{{- end }}
{{- end }}

{{/* How Kubernetes checks the container: an HTTP path if one is given, else the open port. */}}
{{- define "mcp-server.probe" -}}
{{- if .Values.healthPath }}
httpGet:
  path: {{ .Values.healthPath }}
  port: http
{{- else }}
tcpSocket:
  port: http
{{- end }}
{{- end }}

{{/* Every in-cluster name of the Service, plus loopback for port-forwarding. */}}
{{- define "mcp-server.allowedHosts" -}}
{{- $name := include "mcp-server.fullname" . }}
{{- $namespace := .Release.Namespace }}
{{- list $name (printf "%s.%s" $name $namespace) (printf "%s.%s.svc" $name $namespace) (printf "%s.%s.svc.%s" $name $namespace .Values.clusterDomain) "localhost" "127.0.0.1" | join "," }}
{{- end }}
