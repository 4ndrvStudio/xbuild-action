#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'pathname'
require 'xcodeproj'

source_root = Pathname.new(ARGV.fetch(0)).realpath
signing = JSON.parse(File.read(ARGV.fetch(1)))
detected_path = ARGV[2]
detected = detected_path && !detected_path.empty? ? JSON.parse(File.read(detected_path)) : {}

team_id = signing.fetch('team_id')
identity = signing.fetch('certificate_identity')
profiles = signing.fetch('profiles')

detected_by_target = Hash.new { |hash, key| hash[key] = [] }
Array(detected['signable_targets']).each do |target|
  detected_by_target[target['target']] << target['bundle_identifier']
end

def ignored_project?(path)
  lowered = path.each_filename.map(&:downcase)
  lowered.include?('pods') || lowered.include?('deriveddata') || lowered.include?('.git')
end

def expanded_bundle_id(target, configuration, detected_by_target, profiles)
  raw = configuration.build_settings['PRODUCT_BUNDLE_IDENTIFIER'].to_s
  return raw if profiles.key?(raw)

  candidates = detected_by_target[target.name].select { |bundle_id| profiles.key?(bundle_id) }
  return candidates.first if candidates.length == 1

  nil
end

def normalized_project_path(value, source_root)
  return nil if value.nil? || value.empty?

  candidate = Pathname.new(value)
  candidate = source_root.join(candidate) unless candidate.absolute?
  candidate = candidate.realpath
  relative = candidate.relative_path_from(source_root)
  return nil if relative.each_filename.any? { |part| part == '..' }
  return nil unless candidate.extname.downcase == '.xcodeproj' && candidate.directory?
  return nil if ignored_project?(relative)

  candidate
rescue ArgumentError, Errno::ENOENT
  nil
end

project_paths = Array(detected['signable_targets']).map do |target|
  normalized_project_path(target['project_file_path'].to_s, source_root)
end.compact

container_path = detected['container_path'].to_s
container_kind = detected['container_kind'].to_s
if project_paths.empty? && container_kind == 'project'
  project_paths << normalized_project_path(container_path, source_root)
end

if project_paths.empty? && container_kind == 'workspace'
  begin
    workspace_path = Pathname.new(container_path).realpath
    workspace = Xcodeproj::Workspace.new_from_xcworkspace(workspace_path.to_s)
    project_paths.concat(
      workspace.file_references.map do |reference|
        normalized_project_path(
          reference.absolute_path(workspace_path.dirname.to_s),
          source_root
        )
      end.compact
    )
  rescue StandardError => e
    warn "::warning title=Workspace inspection failed::#{e.message}"
  end
end

if project_paths.empty? && !container_path.empty?
  container_directory = Pathname.new(container_path).expand_path.dirname
  Dir.glob(container_directory.join('*.xcodeproj').to_s).sort.each do |path|
    project_paths << normalized_project_path(path, source_root)
  end
end

project_paths = project_paths.compact.uniq

abort('No selected user .xcodeproj was found for signing configuration.') if project_paths.empty?

configured_bundle_ids = []
changed_projects = 0

project_paths.each do |project_path|
  project = Xcodeproj::Project.open(project_path.to_s)
  project_changed = false
  target_attributes = project.root_object.attributes['TargetAttributes'] ||= {}

  project.targets.each do |target|
    next unless target.is_a?(Xcodeproj::Project::Object::PBXNativeTarget)

    product_type = target.respond_to?(:product_type) ? target.product_type.to_s : ''
    next if product_type.include?('unit-test') || product_type.include?('ui-testing')

    target_profile = nil
    target.build_configurations.each do |configuration|
      bundle_id = expanded_bundle_id(target, configuration, detected_by_target, profiles)
      next unless bundle_id

      profile = profiles.fetch(bundle_id)
      target_profile ||= profile
      settings = configuration.build_settings
      settings['DEVELOPMENT_TEAM'] = team_id
      settings['CODE_SIGN_STYLE'] = 'Manual'
      settings['CODE_SIGN_IDENTITY[sdk=iphoneos*]'] = identity
      settings['PROVISIONING_PROFILE_SPECIFIER[sdk=iphoneos*]'] = profile.fetch('uuid')
      settings['PROVISIONING_PROFILE[sdk=iphoneos*]'] = profile.fetch('uuid')
      configured_bundle_ids << bundle_id
      project_changed = true
    end

    next unless target_profile

    attributes = target_attributes[target.uuid] ||= {}
    attributes['DevelopmentTeam'] = team_id
    attributes['ProvisioningStyle'] = 'Manual'
    puts "Configured #{target.name} with profile #{target_profile.fetch('name')}"
  end

  if project_changed
    project.save
    changed_projects += 1
  end
end

missing = profiles.keys - configured_bundle_ids.uniq
unless missing.empty?
  warn "::error title=Signing target not found::Could not apply provisioning profiles to targets for: #{missing.join(', ')}"
  exit 35
end

puts "Configured manual signing in #{changed_projects} Xcode project(s)."
