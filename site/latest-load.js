export function latestMeshLoader(loader, apply) {
  let version = 0;
  return design => new Promise(resolve => {
    const request = ++version;
    loader.load(design.file, geometry => {
      if (request !== version) {
        geometry.dispose();
        resolve(false);
        return;
      }
      apply(geometry, design);
      resolve(true);
    }, undefined, () => resolve(false));
  });
}
