#include "PluginHostBindings.hpp"
#include "slic3r/plugin/PluginBindingUtils.hpp"

#include <libslic3r/PlaceholderParser.hpp>
#include <libslic3r/Preset.hpp>
#include <libslic3r/PresetBundle.hpp>

#include <slic3r/GUI/GUI_App.hpp>

#include <stdexcept>

#include <pybind11/stl.h>

#include <string>
#include <vector>

namespace py = pybind11;

namespace Slic3r {
namespace {

py::list current_filament_presets(PresetBundle& bundle)
{
    py::list presets;
    for (const std::string& preset_name : bundle.filament_presets) {
        Preset* preset = bundle.filaments.find_preset(preset_name);
        if (preset == nullptr)
            presets.append(py::none());
        else
            presets.append(py::cast(preset, py::return_value_policy::reference));
    }
    return presets;
}

PresetCollection& printer_presets(PresetBundle& bundle)
{
    return static_cast<PresetCollection&>(bundle.printers);
}

// --------------------------------------------------------------------------
// orca.host.render_gcode_template
//
// Custom G-code in a profile is a template, not literal G-code: machine_start_gcode,
// change_filament_gcode and friends carry placeholders ({next_extruder}, [layer_z],
// conditionals, arithmetic) that only mean something once resolved against the active
// config. A plugin that emitted one verbatim would write invalid G-code, so rendering
// is done here with the same PlaceholderParser the slicer itself uses.
//
// Variables the slicer would normally supply per call site (next_extruder, layer_z, ...)
// have no meaning outside a running slice, so the caller passes the ones it knows. A
// template referencing anything still undefined fails loudly with the parser's own
// message rather than silently producing broken output.
// --------------------------------------------------------------------------
std::string render_gcode_template(const std::string& templ, const py::dict& variables, unsigned int current_extruder_id)
{
    PresetBundle* bundle = GUI::wxGetApp().preset_bundle;
    if (bundle == nullptr)
        throw std::runtime_error("presets are not loaded yet");

    PlaceholderParser parser;
    parser.apply_config(bundle->full_config());
    parser.update_timestamp();

    // Typed on purpose: a template may compare {next_extruder} numerically, and a
    // string-typed value would make that comparison fail rather than evaluate.
    for (auto item : variables) {
        const std::string key = py::str(item.first).cast<std::string>();
        py::handle        value = item.second;
        if (py::isinstance<py::bool_>(value))
            parser.set(key, value.cast<bool>());
        else if (py::isinstance<py::int_>(value))
            parser.set(key, value.cast<int>());
        else if (py::isinstance<py::float_>(value))
            parser.set(key, value.cast<double>());
        else
            parser.set(key, py::str(value).cast<std::string>());
    }

    try {
        return parser.process(templ, current_extruder_id);
    } catch (const std::exception& exc) {
        throw std::runtime_error(std::string("G-code template failed to render: ") + exc.what());
    }
}

} // namespace

void host_bindings::register_presets(py::module_& host)
{
    py::enum_<Preset::Type>(host, "PresetType")
        .value("Invalid", Preset::TYPE_INVALID)
        .value("Print", Preset::TYPE_PRINT)
        .value("SlaPrint", Preset::TYPE_SLA_PRINT)
        .value("Filament", Preset::TYPE_FILAMENT)
        .value("SlaMaterial", Preset::TYPE_SLA_MATERIAL)
        .value("Printer", Preset::TYPE_PRINTER)
        .value("PhysicalPrinter", Preset::TYPE_PHYSICAL_PRINTER)
        .value("Plate", Preset::TYPE_PLATE)
        .value("Model", Preset::TYPE_MODEL);

    py::class_<Preset, std::unique_ptr<Preset, py::nodelete>>(host, "Preset")
        .def_readonly("type", &Preset::type)
        .def_readonly("name", &Preset::name)
        .def_readonly("alias", &Preset::alias)
        .def_readonly("file", &Preset::file)
        .def_readonly("is_default", &Preset::is_default)
        .def_readonly("is_external", &Preset::is_external)
        .def_readonly("is_system", &Preset::is_system)
        .def_readonly("is_visible", &Preset::is_visible)
        .def_readonly("is_dirty", &Preset::is_dirty)
        .def_readonly("is_compatible", &Preset::is_compatible)
        .def_readonly("is_project_embedded", &Preset::is_project_embedded)
        .def_readonly("bundle_id", &Preset::bundle_id)
        .def("is_user", &Preset::is_user)
        .def("is_from_bundle", &Preset::is_from_bundle)
        .def("label", &Preset::label, py::arg("no_alias") = false)
        .def("config_keys", [](const Preset& preset) { return preset.config.keys(); })
        .def("config_value", [](const Preset& preset, const std::string& key) {
            return config_value_or_none(preset.config, key);
        });

    py::class_<PresetCollection, std::unique_ptr<PresetCollection, py::nodelete>>(host, "PresetCollection")
        .def("size", &PresetCollection::size)
        .def("get_selected_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_selected_preset();
        }, py::return_value_policy::reference_internal)
        .def("selected_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_selected_preset();
        }, py::return_value_policy::reference_internal)
        .def("get_selected_preset_name", &PresetCollection::get_selected_preset_name)
        .def("selected_preset_name", &PresetCollection::get_selected_preset_name)
        .def("get_edited_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("edited_preset", [](PresetCollection& collection) -> Preset& {
            return collection.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("preset", [](PresetCollection& collection, size_t index) -> Preset& {
            if (index >= collection.size())
                throw py::index_error("preset index out of range");
            return collection.preset(index);
        }, py::return_value_policy::reference_internal)
        .def("find_preset", [](PresetCollection& collection, const std::string& name) -> Preset* {
            return collection.find_preset(name);
        }, py::return_value_policy::reference_internal)
        .def("preset_names", [](const PresetCollection& collection) {
            std::vector<std::string> names;
            names.reserve(collection.get_presets().size());
            for (const Preset& preset : collection.get_presets())
                names.push_back(preset.name);
            return names;
        });

    py::class_<PresetBundle, std::unique_ptr<PresetBundle, py::nodelete>>(host, "PresetBundle")
        .def_property_readonly("prints", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.prints;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("printers", &printer_presets, py::return_value_policy::reference_internal)
        .def_property_readonly("filaments", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.filaments;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("sla_prints", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.sla_prints;
        }, py::return_value_policy::reference_internal)
        .def_property_readonly("sla_materials", [](PresetBundle& bundle) -> PresetCollection& {
            return bundle.sla_materials;
        }, py::return_value_policy::reference_internal)
        .def("current_process_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.prints.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_print_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.prints.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_printer_preset", [](PresetBundle& bundle) -> Preset& {
            return bundle.printers.get_edited_preset();
        }, py::return_value_policy::reference_internal)
        .def("current_filament_preset_names", [](PresetBundle& bundle) {
            return bundle.filament_presets;
        })
        .def("current_filament_presets", &current_filament_presets)
        .def("full_config_keys", [](const PresetBundle& bundle) {
            return bundle.full_config().keys();
        })
        .def("full_config_value", [](const PresetBundle& bundle, const std::string& key) {
            return config_value_or_none(bundle.full_config(), key);
        });

    host.def("render_gcode_template", &render_gcode_template, py::arg("template"),
             py::arg("variables") = py::dict(), py::arg("current_extruder_id") = 0,
             "Resolve a profile's custom G-code template (machine_start_gcode, "
             "change_filament_gcode, ...) against the active config, using the slicer's own "
             "PlaceholderParser. `variables` supplies the per-call-site values the slicer "
             "would normally inject (next_extruder, layer_z, ...); ints, floats, bools and "
             "strings keep their type. Raises RuntimeError naming the offending placeholder "
             "if the template references something still undefined.");
}
} // namespace Slic3r
