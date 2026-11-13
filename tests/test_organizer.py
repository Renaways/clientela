from pathlib import Path

from clientela import clients as clients_module
from clientela import organizer
from clientela import projects as projects_module


def test_file_kind_classifies_known_extensions():
    assert organizer.file_kind(Path("Job.prproj")) == "projeto"
    assert organizer.file_kind(Path("Job.aep")) == "projeto"
    assert organizer.file_kind(Path("logo.psd")) == "design"
    assert organizer.file_kind(Path("cam_a.mov")) == "video"
    assert organizer.file_kind(Path("vo.wav")) == "audio"
    assert organizer.file_kind(Path("ref.png")) == "imagem"
    assert organizer.file_kind(Path("Drive.webloc")) == "link"
    assert organizer.file_kind(Path("links.txt")) == "link"
    assert organizer.file_kind(Path("notes.txt")) == "outro"
    assert organizer.file_kind(Path("random.xyz")) == "outro"


def test_file_kind_flags_autosave_files_regardless_of_extension():
    autosave_prproj = Path("079 - violência policial/Adobe Premiere Pro Auto-Save/079--abcd-2026-08-12_16-52-26.prproj")
    autosave_aep = Path("Job/Adobe After Effects Auto-Save/Job Auto-Save.aep")
    assert organizer.file_kind(autosave_prproj) == "autosave"
    assert organizer.file_kind(autosave_aep) == "autosave"


def test_list_project_files_includes_kind_alongside_category(tmp_path):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    project = projects_module.create_project(client_id=client["id"], name="Job")

    files = organizer.list_project_files(project["id"])
    by_name = {f["name"]: f for f in files}

    project_dir = Path(project["path"])
    inner_name = project_dir.name
    assert by_name[f"{inner_name}.prproj"]["kind"] == "projeto"
    assert by_name[f"{inner_name}.prproj"]["category"] == "adobe"
    assert by_name[f"{inner_name}.aep"]["kind"] == "projeto"
    assert by_name["links.txt"]["kind"] == "link"


def test_list_project_files_excludes_autosave_copies_from_projeto_kind(tmp_path):
    client_dir = tmp_path / "Cliente"
    client_dir.mkdir()
    client = clients_module.create_client(name="Cliente", path=str(client_dir))
    project = projects_module.create_project(client_id=client["id"], name="Job")

    project_dir = Path(project["path"])
    inner_name = project_dir.name
    autosave_dir = project_dir / inner_name / "Adobe Premiere Pro Auto-Save"
    autosave_dir.mkdir()
    (autosave_dir / f"{inner_name}--abcd-2026-08-12_16-52-26.prproj").write_text("autosave copy")

    files = organizer.list_project_files(project["id"])
    autosave_file = next(f for f in files if "Auto-Save" in f["path"])
    assert autosave_file["kind"] == "autosave"

    projeto_files = [f for f in files if f["kind"] == "projeto"]
    assert len(projeto_files) == 2  # only the real .prproj and .aep, not the autosave copy
